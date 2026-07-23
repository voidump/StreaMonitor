import glob
import os
import subprocess

from ffmpy import FFmpeg, FFRuntimeError, FFExecutableNotFoundError

import parameters
from parameters import CONTAINER, FFMPEG_PATH
import streamonitor.log as log
from streamonitor.video_filter import remove_undersized_videos


logger = log.Logger("temp_repair").get_logger()

# Leftover temp suffixes produced by the downloaders, mapped to the ffmpeg input
# options needed to remux them. hls.py writes "<name>.tmp.ts", fmp4s_wss.py writes
# "<name>.tmp.mp4" (which needs -ignore_editlist 1, mirroring its post-processing).
TEMP_SUFFIXES = {
    '.tmp.ts': None,
    '.tmp.mp4': '-ignore_editlist 1',
}


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


def _cleanup_failed(tmppath, output_path):
    name = os.path.basename(tmppath)
    _discard_output(output_path)
    try:
        os.remove(tmppath)
    except OSError as e:
        logger.warning(f'Failed to remove temp file {name}: {e}')


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
        # ffmpeg actually ran. A genuine non-zero exit (other than 255, which is
        # ffmpeg's interrupted code) means the temp file itself is unrepairable,
        # so it is safe to remove. On 255/interrupted, keep it for a later retry.
        if e.exit_code and e.exit_code != 255:
            logger.warning(
                f'Repair failed for {name} (ffmpeg exit {e.exit_code}); removing temp file')
            _cleanup_failed(tmppath, output_path)
        else:
            logger.warning(
                f'Repair interrupted for {name} (ffmpeg exit {e.exit_code}); keeping temp file for retry')
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
    else:
        logger.warning(f'Repair produced no output for {name}; keeping temp file for retry')
        _discard_output(output_path)


def repair_leftover_temp_files():
    # Best-effort startup repair of temp files left behind by an interrupted
    # recording. Any failure only logs and never blocks startup.
    try:
        if not parameters.REPAIR_TEMP_ON_START:
            return
        downloads_dir = parameters.get_download_dir()
        if not os.path.isdir(downloads_dir):
            return

        matches = []
        for suffix in TEMP_SUFFIXES:
            for tmppath in glob.glob(os.path.join(downloads_dir, '*', '*' + suffix)):
                matches.append((tmppath, suffix))
        if not matches:
            return

        logger.info(f'Found {len(matches)} leftover temp file(s) to repair')
        for tmppath, suffix in matches:
            try:
                _repair_one(tmppath, TEMP_SUFFIXES[suffix], suffix)
            except Exception as e:
                logger.warning(f'Unexpected error repairing {os.path.basename(tmppath)}: {e}')
    except Exception as e:
        logger.error(f'Temp repair scan failed (ignored): {e}')
