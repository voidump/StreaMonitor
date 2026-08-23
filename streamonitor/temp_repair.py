import glob
import os
import subprocess
import time
from threading import Thread

from ffmpy import FFmpeg, FFRuntimeError, FFExecutableNotFoundError

import parameters
from parameters import CONTAINER, FFMPEG_PATH
import streamonitor.log as log
from streamonitor.media_index import record_repaired_file
from streamonitor.video_filter import remove_undersized_videos


logger = log.Logger("temp_repair").get_logger()

# Leftover temp suffixes produced by the downloaders, mapped to the ffmpeg input
# options needed to remux them. hls.py writes "<name>.tmp.ts", fmp4s_wss.py writes
# "<name>.tmp.mp4" (which needs -ignore_editlist 1, mirroring its post-processing).
TEMP_SUFFIXES = {
    '.tmp.ts': None,
    '.tmp.mp4': '-ignore_editlist 1',
}

# A temp file whose mtime is within this window is treated as actively being
# written and is never touched by the runtime periodic repair.
ACTIVE_MTIME_GRACE = 300  # seconds


def _unique_output_path(base):
    # Never overwrite an existing final file; append a distinguishing suffix.
    candidate = base + '.' + CONTAINER
    if not os.path.exists(candidate):
        return candidate
    candidate = base + '-repaired.' + CONTAINER
    if not os.path.exists(candidate):
        return candidate
    i = 1
    while True:
        candidate = f'{base}-repaired-{i}.{CONTAINER}'
        if not os.path.exists(candidate):
            return candidate
        i += 1


def _discard_output(output_path):
    # Drop a partial/empty remux output while leaving the temp file untouched.
    if output_path and os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass


def _repair_one(tmppath, input_opts, suffix):
    name = os.path.basename(tmppath)
    try:
        size = os.path.getsize(tmppath)
    except OSError as e:
        logger.warning(f'Cannot stat temp file {name}: {e}')
        return

    if size == 0:
        try:
            os.remove(tmppath)
            logger.warning(f'Removed empty temp file: {name}')
        except OSError as e:
            logger.warning(f'Failed to remove empty temp file {name}: {e}')
        return

    output_path = _unique_output_path(tmppath[:-len(suffix)])

    try:
        ff = FFmpeg(
            executable=FFMPEG_PATH,
            inputs={tmppath: input_opts},
            outputs={output_path: '-c:a copy -c:v copy'},
        )
        ff.run(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FFRuntimeError as e:
        # ffmpeg actually ran and failed. The exit code cannot distinguish a
        # corrupt temp file from an environment error — ENOSPC, permissions or
        # a read-only target also exit 1 — so never delete the source here.
        # (The disk-full restart loop used to wipe the only copy of a
        # recording: OOS exit -> restart -> repair -> ENOSPC -> delete.)
        logger.warning(
            f'Repair failed for {name} (ffmpeg exit {e.exit_code}); keeping temp file for retry')
        _discard_output(output_path)
        return
    except FFExecutableNotFoundError as e:
        # ffmpeg is missing/not executable: an environment error, not a bad temp
        # file. Keep the temp file so a later startup (with ffmpeg present) can
        # still repair it.
        logger.warning(f'Cannot repair {name}: ffmpeg not available ({e}); keeping temp file for retry')
        _discard_output(output_path)
        return
    except Exception as e:
        # Other environmental errors (IO errors, etc.): keep the temp file.
        logger.warning(f'Repair error for {name}: {e}; keeping temp file for retry')
        _discard_output(output_path)
        return

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        try:
            os.remove(tmppath)
        except OSError as e:
            logger.warning(
                f'Repaired {name} -> {os.path.basename(output_path)} but failed to remove temp: {e}')
            return
        logger.info(f'Repaired {name} -> {os.path.basename(output_path)}')
        remove_undersized_videos([output_path], logger)
        record_repaired_file(output_path)
    else:
        logger.warning(f'Repair produced no output for {name}; keeping temp file for retry')
        _discard_output(output_path)


def _active_tmp_paths(streamers):
    # Absolute temp paths that could belong to a recording in progress, derived
    # from each recording streamer's final filename. Best-effort: the mtime
    # grace check is the authoritative guard.
    skip = set()
    for streamer in streamers or []:
        try:
            if not getattr(streamer, 'recording', False):
                continue
            name = getattr(streamer, 'recording_filename', None)
            if not name:
                continue
            base = os.path.splitext(name)[0]
            for suffix in TEMP_SUFFIXES:
                skip.add(os.path.abspath(os.path.join(streamer.outputFolder, base + suffix)))
        except Exception:
            continue
    return skip


def _recently_modified(tmppath):
    try:
        return (time.time() - os.path.getmtime(tmppath)) < ACTIVE_MTIME_GRACE
    except OSError:
        return True  # cannot stat -> treat as active, do not touch


def _scan_and_repair(streamers=None, guard_active=False):
    downloads_dir = parameters.get_download_dir()
    if not os.path.isdir(downloads_dir):
        return

    active = _active_tmp_paths(streamers) if guard_active else set()
    matches = []
    for suffix in TEMP_SUFFIXES:
        for tmppath in glob.glob(os.path.join(downloads_dir, '*', '*' + suffix)):
            matches.append((tmppath, suffix))
    if not matches:
        return

    todo = []
    for tmppath, suffix in matches:
        if guard_active and (os.path.abspath(tmppath) in active or _recently_modified(tmppath)):
            logger.debug(f'Skipping active temp file: {os.path.basename(tmppath)}')
            continue
        todo.append((tmppath, suffix))
    if not todo:
        return

    logger.info(f'Found {len(todo)} leftover temp file(s) to repair')
    for tmppath, suffix in todo:
        try:
            _repair_one(tmppath, TEMP_SUFFIXES[suffix], suffix)
        except Exception as e:
            logger.warning(f'Unexpected error repairing {os.path.basename(tmppath)}: {e}')


def repair_leftover_temp_files():
    # Best-effort startup repair of temp files left behind by an interrupted
    # recording. Any failure only logs and never blocks startup.
    try:
        if not parameters.REPAIR_TEMP_ON_START:
            return
        _scan_and_repair()
    except Exception as e:
        logger.error(f'Temp repair scan failed (ignored): {e}')


class TempRepairScanner(Thread):
    # Runtime periodic repair of leftover temp files, one pass every
    # REPAIR_TEMP_INTERVAL seconds. Temp files of an in-progress recording
    # (streamer recording state or a recent mtime) are never touched. Any
    # failure only logs and never affects recording.
    def __init__(self, streamers):
        super().__init__()
        self.daemon = True
        self.streamers = streamers

    def run(self):
        while True:
            # Re-read every cycle so a runtime settings change takes effect;
            # a 60s floor guards against a misconfigured busy loop.
            try:
                interval = int(parameters.REPAIR_TEMP_INTERVAL)
            except Exception:
                interval = 1800
            time.sleep(max(interval, 60))
            if interval <= 0:
                continue
            try:
                _scan_and_repair(self.streamers, guard_active=True)
            except Exception as e:
                logger.error(f'Periodic temp repair failed (ignored): {e}')
