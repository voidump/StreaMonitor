"""In-memory back-scan of videos the NSFW pipeline never analyzed.

The pending set is derived from the media index (classification IS NULL AND
deleted=0), so no task table or resume state is needed: re-triggering after a
restart simply picks up whatever is still unlabeled (idempotent). One session
runs at a time in a single background thread and analyzes one file after the
other, writing the label (adult / non-adult / unknown) as each file finishes.

Labeling only: the configured non-adult action is never triggered here, the
scheduled pass in media_index handles that (and leaves a window for reviewing
misclassifications). Files that already carry a label are never re-analyzed.

Exclusions match apply_nsfw_action: exact recording paths, any
username-prefixed file in a recording streamer's folder, '.tmp.' names, files
modified within CLEANUP_MTIME_GRACE_SECONDS and anything under the recycle bin
or the legacy non-adult archive.
"""
import os
import threading
import time
from contextlib import closing

import parameters
import streamonitor.log as log
import streamonitor.media_index as media_index
from streamonitor.nsfw_analyzer import analyze_file

logger = log.Logger("nsfw_backfill").get_logger()

_lock = threading.Lock()
_thread = None
_cancel = threading.Event()
_state = {
    'running': False,
    'total': 0,
    'done': 0,
    'failed': 0,
    'current_file': None,
    'started_ts': None,
}


def _protected_roots():
    # Recycle bin and legacy non-adult archive; skipped unless they resolve to
    # the download root itself (then everything would be excluded).
    downloads_root = os.path.abspath(parameters.get_download_dir())
    roots = []
    for getter in (parameters.get_nsfw_recycle_dir, parameters.get_nonadult_dir):
        try:
            root = os.path.abspath(getter())
        except Exception:
            continue
        if root != downloads_root:
            roots.append(root + os.sep)
    return roots


def _excluded(abs_path, recording_paths, active, protected, now):
    filename = os.path.basename(abs_path)
    if abs_path in recording_paths or '.tmp.' in filename:
        return True
    if any(abs_path.startswith(prefix) for prefix in protected):
        return True
    try:
        stat = os.stat(abs_path)
    except OSError:
        return True  # vanished; the next scan marks it deleted
    if now - stat.st_mtime < media_index.CLEANUP_MTIME_GRACE_SECONDS:
        return True
    src_folder = os.path.dirname(abs_path)
    return any(src_folder == rec_folder and filename.startswith(rec_user)
               for rec_folder, rec_user in active)


def pending_paths(streamers=None):
    # Absolute paths of unlabeled, non-excluded indexed videos. Empty list when
    # the index cannot be read (never creates the db file).
    if not media_index._index_readable():
        return []
    try:
        with closing(media_index.connect()) as conn:
            rows = conn.execute("SELECT abs_path FROM media_files "
                                "WHERE deleted=0 AND classification IS NULL").fetchall()
    except Exception as e:
        logger.warning(f'NSFW backfill query failed: {e}')
        return []
    recording_paths = media_index._recording_paths(streamers)
    active = media_index._active_recorders(streamers)
    protected = _protected_roots()
    now = time.time()
    return [p for p in (os.path.abspath(row[0]) for row in rows)
            if not _excluded(p, recording_paths, active, protected, now)]


def progress(streamers=None):
    # Snapshot of the current session plus the number of files still waiting.
    with _lock:
        snapshot = dict(_state)
    snapshot['pending'] = len(pending_paths(streamers))
    return snapshot


def _run(streamers):
    try:
        paths = pending_paths(streamers)
        with _lock:
            _state['total'] = len(paths)
        for abs_path in paths:
            if _cancel.is_set() or not parameters.get_nsfw_enabled():
                break
            with _lock:
                _state['current_file'] = os.path.basename(abs_path)
            recording_paths = media_index._recording_paths(streamers)
            active = media_index._active_recorders(streamers)
            if _excluded(abs_path, recording_paths, active, _protected_roots(), time.time()):
                # Became ineligible since the snapshot (recording restarted,
                # moved, deleted); drop it from the total instead of failing.
                with _lock:
                    _state['total'] = max(_state['total'] - 1, _state['done'] + _state['failed'])
                continue
            try:
                label, _scores = analyze_file(abs_path)
                media_index.set_classification(abs_path, label)
                key = 'done'
            except Exception as e:
                logger.warning(f'NSFW backfill failed for {abs_path}: {e}')
                key = 'failed'
            with _lock:
                _state[key] += 1
    except Exception as e:
        logger.warning(f'NSFW backfill aborted: {e}')
    finally:
        with _lock:
            _state['running'] = False
            _state['current_file'] = None
        logger.info(f"NSFW backfill finished: {_state['done']} labeled, "
                    f"{_state['failed']} failed of {_state['total']}")


def start(streamers=None):
    # Starts a session unless one is already running (then the current state is
    # returned unchanged). Returns (progress, started).
    global _thread
    with _lock:
        if _state['running']:
            started = False
        else:
            _cancel.clear()
            _state.update({'running': True, 'total': 0, 'done': 0, 'failed': 0,
                           'current_file': None, 'started_ts': int(time.time())})
            started = True
    if started:
        _thread = threading.Thread(target=_run, args=(streamers,), daemon=True)
        _thread.start()
    return progress(streamers), started


def cancel(streamers=None):
    # Stops after the file being analyzed finishes; a no-op when idle.
    _cancel.set()
    return progress(streamers)
