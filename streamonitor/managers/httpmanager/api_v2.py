import hashlib
import importlib.util
import json
import math
import os
import queue
import re
import subprocess
import threading
from contextlib import closing
from secrets import compare_digest
from time import sleep, time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

import requests
from flask import Response, request, send_file

import parameters
import streamonitor.config as config
from parameters import WEB_LIST_FREQUENCY, FFMPEG_PATH
from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.enums import Status
import streamonitor.media_index as media_index
import streamonitor.online_history as online_history
from streamonitor.managers.outofspace_detector import OOSDetector
from streamonitor.utils import human_file_size


# Stable machine-readable keys derived from the backend Status enum. The web UI
# combines these with `running` and `monitorState` to derive paused/stopped.
_STATUS_KEYS = {
    Status.PUBLIC: 'online',
    Status.OFFLINE: 'offline',
    Status.LONG_OFFLINE: 'long_offline',
    Status.PRIVATE: 'private',
    Status.RATELIMIT: 'ratelimit',
    Status.ERROR: 'error',
    Status.RESTRICTED: 'restricted',
    Status.NOTEXIST: 'notexist',
    Status.NOTRUNNING: 'notrunning',
    Status.UNKNOWN: 'unknown',
}


def _status_key(sc):
    return _STATUS_KEYS.get(sc, 'unknown')


# Live view (watch-while-recording) tuning. We start the tail a bit before the
# write head so latency stays low. For raw MPEG-TS the start is aligned to a
# 188-byte packet boundary; for fragmented MP4 (what StripChat/CMAF actually
# writes into the .tmp.ts) we prepend the init segment and start at a recent
# fragment boundary. These are module-level so tests can shrink them.
_TS_PACKET_SIZE = 188
_LIVE_TAIL_BYTES = 1 << 20  # ~1 MB backoff from the write head
_LIVE_READ_CHUNK = 64 * 1024
_LIVE_POLL_INTERVAL = 0.5  # seconds between reads when caught up to the tail
_LIVE_MAX_IDLE_ITERS = 60  # give up after this many empty reads (~30s)

# Delay after a change-password attempt with the wrong current password, the
# same cheap brute-force deterrent the login route uses. Module-level so tests
# can shrink it.
PASSWORD_FAILURE_DELAY = 1


def _fmp4_init_end(f):
    # Parse ISO-BMFF top-level boxes from the start and return the offset just
    # past the 'moov' box (the init segment = ftyp + ... + moov). None if not
    # found within a sane number of boxes.
    f.seek(0)
    offset = 0
    for _ in range(64):
        header = f.read(8)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[0:4], 'big')
        box_type = header[4:8]
        if size == 1:  # 64-bit largesize
            ext = f.read(8)
            if len(ext) < 8:
                return None
            size = int.from_bytes(ext, 'big')
        if size < 8:
            return None
        if box_type == b'moov':
            return offset + size
        offset += size
        f.seek(offset)
    return None


def _fmp4_last_moof(window, base):
    # Find the absolute offset of the last 'moof' box in `window` that is
    # immediately followed by an 'mdat' box (a self-contained CMAF fragment).
    i = 0
    last = None
    while True:
        j = window.find(b'moof', i)
        if j < 0:
            break
        if j >= 4:
            size = int.from_bytes(window[j - 4:j], 'big')
            nxt = (j - 4) + size
            if 8 <= size and nxt + 8 <= len(window) and window[nxt + 4:nxt + 8] == b'mdat':
                last = base + (j - 4)
        i = j + 4
    return last


def _live_tmp_path(streamer):
    # Mirrors getVideoNativeHLS naming: final "<name>.<container>" is written to
    # "<name>.tmp.ts" while recording. Returns None when not applicable.
    if not getattr(streamer, 'recording', False):
        return None
    recording_name = getattr(streamer, 'recording_filename', None)
    if not recording_name:
        return None
    base, _ext = os.path.splitext(recording_name)
    tmp_name = base + '.tmp.ts'
    return os.path.join(streamer.outputFolder, tmp_name)


def _streamer_summary(streamer, index_stats=None):
    tmp_path = _live_tmp_path(streamer)
    # Most recent recording time for the list card (NB15). Prefer the media
    # index aggregate when the caller provides one (single SQLite query for the
    # whole list); otherwise reuse the already cached video_files enumeration
    # (same as videoCount/totalSize) so the status poll never adds a fresh
    # directory scan; epoch seconds, or None when empty.
    if index_stats is not None:
        video_count = index_stats['count']
        total_size = index_stats['total_size']
        last_recorded_ts = index_stats['last_mtime']
    else:
        video_files = streamer.video_files
        video_count = len(video_files)
        total_size = streamer.video_files_total_size
        last_recorded_ts = int(max(v._stat.st_mtime for v in video_files)) if video_files else None
    return {
        'site': streamer.site,
        'siteslug': streamer.siteslug,
        'username': streamer.username,
        'url': streamer.url,
        'monitorState': getattr(streamer, 'monitorState', 'active'),
        'running': streamer.running,
        'status': _status_key(streamer.sc),
        'statusText': streamer.status(),
        'recording': streamer.recording,
        'liveAvailable': bool(tmp_path and os.path.exists(tmp_path)),
        'recordingFilename': getattr(streamer, 'recording_filename', None),
        'recordingBytes': getattr(streamer, 'recording_bytes', 0),
        # Live speed/resolution only while recording; null otherwise and for
        # sites that never resolve a resolution (frontend degrades gracefully).
        'recordingSpeedBps': getattr(streamer, 'recording_speed_bps', None) if streamer.recording else None,
        'recordingResolution': getattr(streamer, 'recording_resolution', None) if streamer.recording else None,
        'videoCount': video_count,
        'totalSize': total_size,
        'lastCheckTs': getattr(streamer, 'lastCheckTs', None),
        'lastRecordedTs': last_recorded_ts,
    }


def _json(payload, status_code=200):
    return Response(json.dumps(payload), status=status_code, mimetype='application/json')


def _download_dir_payload():
    raw = parameters.get_download_dir()
    abspath = os.path.abspath(os.path.expanduser(raw))
    usage = OOSDetector.space_usage()
    return {
        'path': abspath,
        'raw': raw,
        'diskSpace': {
            'total': usage.total,
            'free': usage.free,
            'used': usage.total - usage.free,
            'freeHuman': human_file_size(usage.free),
            'totalHuman': human_file_size(usage.total),
        },
    }


def _validate_download_dir(raw):
    # Returns (abspath, None) on success or (None, (code, message)) on failure.
    if not isinstance(raw, str) or not raw.strip():
        return None, ('empty', 'Path must not be empty')
    candidate = os.path.abspath(os.path.expanduser(raw.strip()))
    try:
        os.makedirs(candidate, exist_ok=True)
    except Exception as e:
        return None, ('create_failed', f'Cannot create directory: {e}')
    if not os.path.isdir(candidate):
        return None, ('not_dir', 'Path is not a directory')
    probe = os.path.join(candidate, '.strmntr_write_test')
    try:
        with open(probe, 'w') as f:
            f.write('')
        os.remove(probe)
    except Exception:
        return None, ('not_writable', 'Directory is not writable')
    return candidate, None


# Recording resolution runtime setting (V33A). resolution is "max" (always pick
# the source's highest variant) or an integer height in [_RESOLUTION_MIN,
# _RESOLUTION_MAX]; preference is one of parameters.RESOLUTION_PREFERENCES and is
# ignored when resolution is "max".
_RESOLUTION_MIN = 144
_RESOLUTION_MAX = 9999


def _recording_resolution_payload():
    return {
        'resolution': parameters.get_recording_resolution(),
        'preference': parameters.get_recording_resolution_preference(),
    }


def _nsfw_runtime_available():
    # find_spec only locates the packages, it never imports them; it can still
    # raise (e.g. a broken installation), which must degrade to "unavailable"
    # rather than turning the settings endpoint into a 500.
    try:
        return (importlib.util.find_spec('onnxruntime') is not None
                and importlib.util.find_spec('numpy') is not None)
    except Exception:
        return False


def _nsfw_payload():
    # Readiness is informational only: the toggle may be enabled without the
    # model or runtime packages (analysis then degrades to 'unknown' labels).
    model_path = parameters.NSFW_MODEL_PATH
    return {
        'enabled': parameters.get_nsfw_enabled(),
        'action': parameters.get_nsfw_action(),
        'recycleRetentionDays': parameters.get_nsfw_recycle_retention_days(),
        'scanIntervalHours': parameters.get_nsfw_scan_interval_hours(),
        'threshold': parameters.get_nsfw_threshold(),
        'modelPresent': os.path.isfile(model_path),
        'modelPath': model_path,
        'runtimeAvailable': _nsfw_runtime_available(),
    }


def _validate_nsfw_settings(data):
    # Returns (updates, None) on success or (None, (code, message)) on
    # failure. `enabled` is required; the sub-options are optional so old
    # clients that only send {enabled} keep working. Nothing is applied
    # unless the whole payload validates (400 never changes state).
    if not isinstance(data, dict):
        return None, ('invalid', 'enabled must be a boolean')
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return None, ('invalid', 'enabled must be a boolean')
    updates = {'enabled': enabled}
    if 'action' in data:
        action = data.get('action')
        if action not in parameters.NSFW_ACTIONS:
            return None, ('invalid', 'action must be one of: ' + ', '.join(parameters.NSFW_ACTIONS))
        updates['action'] = action
    if 'recycleRetentionDays' in data:
        days = data.get('recycleRetentionDays')
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            return None, ('invalid', 'recycleRetentionDays must be an integer between 1 and 90')
        updates['recycleRetentionDays'] = days
    if 'scanIntervalHours' in data:
        hours = data.get('scanIntervalHours')
        if isinstance(hours, bool) or not isinstance(hours, int) or not 0 <= hours <= 168:
            return None, ('invalid', 'scanIntervalHours must be an integer between 0 and 168')
        updates['scanIntervalHours'] = hours
    if 'threshold' in data:
        threshold = data.get('threshold')
        _bad_threshold = ('invalid', 'threshold must be a number between 0 and 1')
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            return None, _bad_threshold
        threshold = float(threshold)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            return None, _bad_threshold
        updates['threshold'] = threshold
    return updates, None


def _validate_recording_resolution(data):
    # Returns (resolution, preference, None) on success or
    # (None, None, (code, message)) on failure.
    if not isinstance(data, dict) or 'resolution' not in data:
        return None, None, ('missing', 'resolution is required')
    raw = data.get('resolution')
    _invalid = ('invalid_resolution', 'resolution must be "max" or an integer height')
    if isinstance(raw, str) and raw.strip().lower() == parameters.RECORDING_RESOLUTION_MAX:
        resolution = parameters.RECORDING_RESOLUTION_MAX
    else:
        if isinstance(raw, bool):
            return None, None, _invalid
        if isinstance(raw, int):
            resolution = raw
        elif isinstance(raw, float):
            if not raw.is_integer():
                return None, None, _invalid
            resolution = int(raw)
        elif isinstance(raw, str):
            try:
                resolution = int(raw.strip())
            except (TypeError, ValueError):
                return None, None, _invalid
        else:
            return None, None, _invalid
        if resolution < _RESOLUTION_MIN or resolution > _RESOLUTION_MAX:
            return None, None, ('invalid_resolution',
                                f'resolution must be between {_RESOLUTION_MIN} and {_RESOLUTION_MAX}')
    preference = data.get('preference')
    if preference is None:
        preference = parameters.get_recording_resolution_preference()
    elif preference not in parameters.RESOLUTION_PREFERENCES:
        return None, None, ('invalid_preference',
                            'preference must be one of ' + ', '.join(parameters.RESOLUTION_PREFERENCES))
    return resolution, preference, None


# Video thumbnail + duration extraction (NB6). Frames and durations are cached
# under "<download dir>/.thumbs/" keyed by absolute source path + mtime, so a
# changed/re-recorded file regenerates automatically and status polls never
# re-run ffprobe on the whole library. The cache dir lives next to the streamer
# folders but is never scanned as a recording (cache_file_list only reads a
# streamer's own folder and skips subdirectories).
_THUMB_WIDTH = 480
_THUMB_TIMEOUT = 30  # seconds; a broken file fails ffmpeg fast, well under this
_FFPROBE_TIMEOUT = 20


def _ffprobe_bin():
    # Derive the ffprobe binary from the configured ffmpeg path so a custom
    # install location keeps working; default ('ffmpeg' on PATH) -> 'ffprobe'.
    directory = os.path.dirname(FFMPEG_PATH)
    base = os.path.basename(FFMPEG_PATH)
    base = 'ffprobe' + base[len('ffmpeg'):] if base.startswith('ffmpeg') else 'ffprobe'
    return os.path.join(directory, base) if directory else base


def _thumbs_dir():
    # Absolute so send_file (which resolves relative paths against the Flask app
    # root, not the CWD) reads the cached frame correctly.
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs'))


def _cache_key(abs_path, mtime):
    return hashlib.sha1(f'{abs_path}:{mtime}'.encode('utf-8')).hexdigest()


def _thumb_cache_path(abs_path, mtime):
    return os.path.join(_thumbs_dir(), _cache_key(abs_path, mtime) + '.jpg')


# All probed durations live in one consolidated JSON file (keyed by the same
# path+mtime hash used for thumbs) instead of one tiny file per video. This is
# loaded lazily on first access and kept in memory, so a service restart with a
# warm cache never re-runs ffprobe over the whole library. A null value means a
# prior probe failed and is cached to avoid re-probing broken files.
_DURATIONS_FILE = 'durations.json'
_durations_lock = threading.Lock()
_durations_cache = None          # dict key -> duration|null once loaded
_durations_cache_path = None     # which durations.json the cache was loaded from

# Stale-key cleanup. durations.json accumulates one entry per (path, mtime); when
# a file is renamed, deleted, or rewritten (new mtime -> new key) the old entry is
# never referenced again and the file grows without bound. We prune entries whose
# key no longer matches any live video file. The sweep enumerates every streamer's
# folder, so it is gated behind a size threshold and a growth margin to keep it off
# the hot path: it only runs once the cache exceeds _DURATIONS_MAX_KEYS and has
# grown _DURATIONS_CLEANUP_GROWTH keys since the previous sweep.
_DURATIONS_MAX_KEYS = 2000
_DURATIONS_CLEANUP_GROWTH = 200
_durations_last_sweep_size = 0


def _durations_path():
    return os.path.join(_thumbs_dir(), _DURATIONS_FILE)


def _load_durations_locked(path):
    # Caller holds _durations_lock. Reads path into the in-memory cache; a
    # missing or corrupt file is tolerated and treated as an empty cache.
    global _durations_cache, _durations_cache_path
    data = {}
    try:
        with open(path) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
    _durations_cache = data
    _durations_cache_path = path


def _save_durations_locked(path):
    # Caller holds _durations_lock. Atomic write via temp file + os.replace so a
    # crash mid-write never leaves a truncated durations.json behind.
    try:
        os.makedirs(_thumbs_dir(), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(_durations_cache, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _probe_duration(abs_path):
    # Returns duration in seconds (float) or None when the file is unreadable.
    try:
        result = subprocess.run(
            [_ffprobe_bin(), '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', abs_path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=_FFPROBE_TIMEOUT)
        duration = float(result.stdout.decode('utf-8', 'ignore').strip())
        if duration > 0:
            return duration
    except Exception:
        pass
    return None


def _get_duration_cached(abs_path, mtime):
    key = _cache_key(abs_path, mtime)
    path = _durations_path()
    with _durations_lock:
        if _durations_cache is None or _durations_cache_path != path:
            _load_durations_locked(path)
        if key in _durations_cache:
            return _durations_cache[key]
    duration = _probe_duration(abs_path)
    with _durations_lock:  # cache failures too (None) to avoid re-probing broken files
        if _durations_cache is None or _durations_cache_path != path:
            _load_durations_locked(path)
        _durations_cache[key] = duration
        _save_durations_locked(path)
    return duration


def _get_duration_cache_only(abs_path, mtime):
    # Read a previously probed duration from the warm cache without ever probing.
    # Used by the schedule backfill so reconstructing history from a large library
    # never triggers a mass ffprobe; uncached files simply return None.
    key = _cache_key(abs_path, mtime)
    path = _durations_path()
    with _durations_lock:
        if _durations_cache is None or _durations_cache_path != path:
            _load_durations_locked(path)
        return _durations_cache.get(key)


def rekey_video_caches(old_abs, new_abs, mtime):
    # A moved file (same content, same mtime) gets a new cache key because the
    # key hashes abs_path+mtime. Migrate the cached duration entry and rename
    # the cached thumbnail so the move never triggers a re-probe or a thumbnail
    # regeneration. Returns the new thumbnail path when the cached frame exists
    # on disk after the migration, else None. Best effort; never raises.
    mtime = int(mtime)
    old_key = _cache_key(old_abs, mtime)
    new_key = _cache_key(new_abs, mtime)
    path = _durations_path()
    try:
        with _durations_lock:
            if _durations_cache is None or _durations_cache_path != path:
                _load_durations_locked(path)
            if old_key in _durations_cache and new_key not in _durations_cache:
                _durations_cache[new_key] = _durations_cache.pop(old_key)
                _save_durations_locked(path)
    except Exception:
        pass
    old_thumb = _thumb_cache_path(old_abs, mtime)
    new_thumb = _thumb_cache_path(new_abs, mtime)
    try:
        if os.path.exists(old_thumb) and not os.path.exists(new_thumb):
            os.replace(old_thumb, new_thumb)
    except Exception:
        pass
    return new_thumb if os.path.exists(new_thumb) else None


# Background duration probing. The detail endpoint must never block on ffprobe,
# so uncached videos are queued here and probed by a single daemon worker that
# writes results into the in-memory cache immediately and batches durations.json
# writes (every _PROBE_BATCH_SAVE probes, plus a flush when the queue drains)
# instead of rewriting the whole file per probe. A dedup set keeps a file from
# being queued twice while it is pending or in flight.
_PROBE_BATCH_SAVE = 20
_probe_queue = queue.Queue()
_probe_queued = set()
_probe_queued_lock = threading.Lock()
_probe_worker_thread = None
_probe_worker_lock = threading.Lock()


def _duration_probe_worker():
    pending_saves = 0
    while True:
        abs_path, mtime = _probe_queue.get()
        key = _cache_key(abs_path, mtime)
        try:
            path = _durations_path()
            with _durations_lock:
                if _durations_cache is None or _durations_cache_path != path:
                    _load_durations_locked(path)
                already = key in _durations_cache
            if not already:
                duration = _probe_duration(abs_path)  # cache None too (broken file)
                with _durations_lock:
                    if _durations_cache is None or _durations_cache_path != path:
                        _load_durations_locked(path)
                    _durations_cache[key] = duration
                pending_saves += 1
            # Flush before signalling task_done so queue.join() guarantees the
            # batch is on disk (every _PROBE_BATCH_SAVE probes, or when drained).
            if pending_saves and (pending_saves >= _PROBE_BATCH_SAVE or _probe_queue.empty()):
                with _durations_lock:
                    _save_durations_locked(_durations_path())
                pending_saves = 0
        except Exception:
            pass
        finally:
            with _probe_queued_lock:
                _probe_queued.discard(key)
            _probe_queue.task_done()


def _ensure_probe_worker():
    global _probe_worker_thread
    with _probe_worker_lock:
        if _probe_worker_thread is None or not _probe_worker_thread.is_alive():
            _probe_worker_thread = threading.Thread(
                target=_duration_probe_worker, name='duration-probe', daemon=True)
            _probe_worker_thread.start()


def enqueue_duration_probe(abs_path, mtime):
    # Fire-and-forget: queue a video for background duration probing so callers
    # never block on ffprobe. No-op when the duration is already cached (a cached
    # null counts, so broken files are not re-probed) or already pending. Never
    # raises so recording teardown and request handling stay unaffected.
    try:
        mtime = int(mtime)
        key = _cache_key(abs_path, mtime)
        path = _durations_path()
        with _durations_lock:
            if _durations_cache is None or _durations_cache_path != path:
                _load_durations_locked(path)
            if key in _durations_cache:
                return
        with _probe_queued_lock:
            if key in _probe_queued:
                return
            _probe_queued.add(key)
        _ensure_probe_worker()
        _probe_queue.put((abs_path, mtime))
    except Exception:
        pass


def _live_duration_keys(streamers):
    # Set of cache keys for every video file that currently exists across all
    # streamers, derived exactly like _get_duration_cached (abs_path + int(mtime)).
    # Runs without _durations_lock: refreshing each streamer's listing touches the
    # disk and must not block duration lookups on the hot path.
    keys = set()
    for streamer in streamers:
        try:
            streamer.cache_file_list()
            for video in streamer.video_files:
                keys.add(_cache_key(video.abs_path, int(video._stat.st_mtime)))
        except Exception:
            continue
    return keys


def _prune_durations(valid_keys):
    # Drop cached durations whose key is absent from valid_keys (stale renamed or
    # deleted files). Reuses the existing lock and atomic write. Returns the number
    # of entries removed.
    path = _durations_path()
    with _durations_lock:
        if _durations_cache is None or _durations_cache_path != path:
            _load_durations_locked(path)
        stale = [k for k in _durations_cache if k not in valid_keys]
        for key in stale:
            del _durations_cache[key]
        if stale:
            _save_durations_locked(path)
        return len(stale)


def _maybe_cleanup_durations(streamers):
    # Threshold-gated stale-key sweep. Enumerating every streamer's folder is only
    # worthwhile once durations.json has grown large, so we skip it entirely below
    # the threshold and re-run only after meaningful growth since the last sweep,
    # which keeps repeated detail requests from re-scanning a big library.
    global _durations_last_sweep_size
    path = _durations_path()
    with _durations_lock:
        if _durations_cache is None or _durations_cache_path != path:
            _load_durations_locked(path)
        size = len(_durations_cache)
        if size <= _DURATIONS_MAX_KEYS or \
                size - _durations_last_sweep_size < _DURATIONS_CLEANUP_GROWTH:
            return
    _prune_durations(_live_duration_keys(streamers))
    with _durations_lock:
        _durations_last_sweep_size = \
            len(_durations_cache) if _durations_cache is not None else 0


def _generate_thumb(abs_path, out_path, duration=None):
    # Extract one frame ~10% into the video (fallback 5s) scaled to _THUMB_WIDTH.
    # Writes atomically via a temp file; returns True on success.
    if duration is None:
        duration = _probe_duration(abs_path)
    seek = 5.0
    if duration and duration > 0:
        seek = min(max(duration * 0.1, 1.0), max(duration - 0.5, 0.0))
    tmp = out_path + '.tmp.jpg'
    try:
        os.makedirs(_thumbs_dir(), exist_ok=True)
        result = subprocess.run(
            [FFMPEG_PATH, '-y', '-ss', str(seek), '-i', abs_path,
             '-frames:v', '1', '-an', '-vf', f'scale={_THUMB_WIDTH}:-2',
             '-q:v', '3', tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_THUMB_TIMEOUT)
        if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out_path)
            return True
    except Exception:
        pass
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


# Thumbnail generation is CPU-heavy (one ffmpeg per frame extraction). Opening a
# detail page with many uncached videos would otherwise spawn one ffmpeg per
# concurrent /thumb request and thrash the CPU, so every generation (on-demand
# and background prewarm alike) passes through one shared semaphore. A per-file
# in-flight guard collapses duplicate concurrent requests for the same file:
# only one thread generates while the others wait and then serve the result.
_THUMB_MAX_CONCURRENCY = 2
_thumb_semaphore = threading.Semaphore(_THUMB_MAX_CONCURRENCY)
_thumb_inflight = {}
_thumb_inflight_lock = threading.Lock()


def _generate_thumb_limited(abs_path, thumb_path, mtime, duration=None,
                            allow_probe=True):
    # Concurrency-limited, dedup-guarded wrapper around _generate_thumb. Returns
    # True when the thumbnail exists on disk (already cached or freshly made).
    # The cached fast path takes no lock and no semaphore so it stays as cheap as
    # a plain os.path.exists check. allow_probe=False (background prewarm) reads
    # the duration cache only and never triggers an ffprobe, keeping all probing
    # centralized in the duration worker; an unavailable duration just falls back
    # to the fixed seek in _generate_thumb.
    if os.path.exists(thumb_path):
        return True
    key = _cache_key(abs_path, mtime)
    with _thumb_inflight_lock:
        event = _thumb_inflight.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _thumb_inflight[key] = event
    if not owner:
        # Another thread is already generating this exact file: wait for it and
        # then serve whatever it produced instead of running a duplicate ffmpeg.
        event.wait(_THUMB_TIMEOUT + 5)
        return os.path.exists(thumb_path)
    try:
        with _thumb_semaphore:
            # Re-check after acquiring: the file may have appeared meanwhile.
            if os.path.exists(thumb_path):
                return True
            if duration is None:
                duration = _get_duration_cached(abs_path, mtime) if allow_probe \
                    else _get_duration_cache_only(abs_path, mtime)
            # Pass 0 (not None) when unavailable so _generate_thumb never probes
            # on its own: probing stays the duration worker's job.
            return _generate_thumb(abs_path, thumb_path, duration or 0)
    finally:
        with _thumb_inflight_lock:
            _thumb_inflight.pop(key, None)
        event.set()


# Background thumbnail prewarming. When a detail page is opened, every video that
# is missing a cached thumbnail is queued here so a single low-priority daemon
# worker generates them ahead of the browser's own requests. The worker shares
# the _thumb_semaphore above, so prewarm and on-demand generation never exceed
# the concurrency cap together. A dedup set keeps a file from being queued twice.
_thumb_prewarm_queue = queue.Queue()
_thumb_prewarm_queued = set()
_thumb_prewarm_queued_lock = threading.Lock()
_thumb_prewarm_thread = None
_thumb_prewarm_worker_lock = threading.Lock()


def _thumb_prewarm_worker():
    while True:
        abs_path, thumb_path, mtime = _thumb_prewarm_queue.get()
        key = _cache_key(abs_path, mtime)
        try:
            _generate_thumb_limited(abs_path, thumb_path, mtime, allow_probe=False)
        except Exception:
            pass
        finally:
            with _thumb_prewarm_queued_lock:
                _thumb_prewarm_queued.discard(key)
            _thumb_prewarm_queue.task_done()


def _ensure_thumb_prewarm_worker():
    global _thumb_prewarm_thread
    with _thumb_prewarm_worker_lock:
        if _thumb_prewarm_thread is None or not _thumb_prewarm_thread.is_alive():
            _thumb_prewarm_thread = threading.Thread(
                target=_thumb_prewarm_worker, name='thumb-prewarm', daemon=True)
            _thumb_prewarm_thread.start()


def enqueue_thumb_prewarm(abs_path, thumb_path, mtime):
    # Fire-and-forget: queue a video for background thumbnail generation. No-op
    # when the thumbnail already exists or is already pending. Never raises so
    # request handling stays unaffected.
    try:
        mtime = int(mtime)
        if os.path.exists(thumb_path):
            return
        key = _cache_key(abs_path, mtime)
        with _thumb_prewarm_queued_lock:
            if key in _thumb_prewarm_queued:
                return
            _thumb_prewarm_queued.add(key)
        _ensure_thumb_prewarm_worker()
        _thumb_prewarm_queue.put((abs_path, thumb_path, mtime))
    except Exception:
        pass


# Real on-site model avatar (NB12). "Show the model the way their site shows
# them." The URL is pulled from the bot's live lastInfo via a small per-site
# extractor; the image is proxied through the bot's own session (so cookies /
# proxy / referer that defeat hotlink+CORS apply) and cached to
# "<download dir>/.thumbs/avatars/" keyed by site:user with a TTL. When the bot
# is offline (lastInfo empty) a previously cached avatar is still served; sites
# with no known extractor, or any failure, 404 so the frontend falls back to its
# generated placeholder.
_AVATAR_TTL = 24 * 60 * 60  # seconds; refetch at most once a day
_AVATAR_TIMEOUT = 15
_AVATAR_MAX_BYTES = 8 * 1024 * 1024


def _avatar_from_stripchat(info):
    # getStatus stores {'model': <user dict>, ...cam}. The user dict carries the
    # profile picture (avatarUrl) and a live snapshot (previewUrl).
    model = info.get('model') if isinstance(info, dict) else None
    for src in (model, info):
        if isinstance(src, dict):
            for key in ('avatarUrl', 'previewUrl', 'snapshotUrl'):
                url = src.get(key)
                if url:
                    return url
    return None


def _avatar_from_chaturbate(info):
    if isinstance(info, dict):
        for key in ('img', 'image_url', 'imageUrl'):
            url = info.get(key)
            if url:
                return url
    return None


# Keyed by Bot.siteslug. Unknown sites intentionally absent -> no avatar.
_AVATAR_EXTRACTORS = {
    'SC': _avatar_from_stripchat,
    'SCVR': _avatar_from_stripchat,
    'CB': _avatar_from_chaturbate,
}


def _avatar_source_url(streamer):
    extractor = _AVATAR_EXTRACTORS.get(getattr(streamer, 'siteslug', None))
    if extractor is None:
        return None
    info = getattr(streamer, 'lastInfo', None)
    if not info:
        return None
    url = extractor(info)
    if not url:
        return None
    if url.startswith('//'):
        url = 'https:' + url
    return url


def _avatars_dir():
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs', 'avatars'))


def _avatar_cache_path(site, user):
    key = hashlib.sha1(f'{site}:{user}'.encode('utf-8')).hexdigest()
    return os.path.join(_avatars_dir(), key + '.img')


def _sniff_image_mime(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return 'image/jpeg'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def _download_avatar(streamer, url, out_path):
    # Fetch through the bot's session (its headers/cookies/proxy) and cache
    # atomically. Returns True on success. Only image/* responses are accepted.
    try:
        os.makedirs(_avatars_dir(), exist_ok=True)
        r = streamer.session.get(
            url, headers=streamer.headers, cookies=streamer.cookies,
            timeout=_AVATAR_TIMEOUT)
        if r.status_code != 200 or not r.content:
            return False
        if len(r.content) > _AVATAR_MAX_BYTES:
            return False
        ctype = r.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if ctype and not ctype.startswith('image/'):
            return False
        tmp = out_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(r.content)
        os.replace(tmp, out_path)
        return True
    except Exception:
        return False


# Real site favicon for the site badge (NB11). "Show each site the way it brands
# itself." The domain is derived from the site's own getWebsiteURL (a live
# streamer's when available, else the site class with a placeholder instance) and
# the icon is cached to "<download dir>/.thumbs/favicons/" keyed by siteslug with
# a long TTL. Fetch order: /favicon.ico -> <link rel=icon> in the homepage HTML
# -> Google's favicon service as a last resort. Unknown sites or a total failure
# 404 so the frontend falls back to its colored dot + abbreviation.
_FAVICON_TTL = 7 * 24 * 60 * 60  # seconds; refetch at most once a week
_FAVICON_TIMEOUT = 10
_FAVICON_MAX_BYTES = 1024 * 1024


def _favicons_dir():
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs', 'favicons'))


def _favicon_cache_path(site):
    key = hashlib.sha1(site.encode('utf-8')).hexdigest()
    return os.path.join(_favicons_dir(), key + '.img')


def _favicon_mime(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return 'image/x-icon'
    if head.startswith(b'\x00\x00\x01\x00'):
        return 'image/x-icon'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.lstrip()[:4] == b'<svg' or head.startswith(b'<?xml'):
        return 'image/svg+xml'
    return 'image/x-icon'


def _site_domain(manager, site_cls):
    # Prefer a live streamer's URL (it already knows room_id and such); otherwise
    # ask the site class with a placeholder instance. Returns a bare host or None.
    url = None
    for s in manager.streamers:
        if type(s) is site_cls:
            try:
                url = s.getWebsiteURL()
            except Exception:
                url = None
            if url:
                break
    if not url:
        try:
            url = site_cls.getWebsiteURL(SimpleNamespace(username='_', room_id=None))
        except Exception:
            url = None
    if not url:
        return None
    return urlparse(url).netloc or None


def _favicon_http_get(url):
    r = requests.get(
        url, headers={'User-Agent': parameters.HTTP_USER_AGENT}, timeout=_FAVICON_TIMEOUT)
    if r.status_code != 200 or not r.content:
        return None, None
    ctype = r.headers.get('Content-Type', '').split(';')[0].strip().lower()
    return r.content, ctype


def _favicon_from_html(domain):
    try:
        content, ctype = _favicon_http_get('https://' + domain + '/')
    except Exception:
        return None
    if not content or (ctype and not ctype.startswith('text/html')):
        return None
    html = content.decode('utf-8', 'ignore')
    tag = re.search(r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*>', html, re.I)
    if not tag:
        return None
    href = re.search(r'href=["\']([^"\']+)["\']', tag.group(0), re.I)
    if not href:
        return None
    return urljoin('https://' + domain + '/', href.group(1))


def _fetch_favicon(domain):
    # Returns raw image bytes or None. Tries the site's own icon first, then a
    # Google favicon-service fallback so a recognizable icon is (almost) always
    # returned. Centralized here so tests can monkeypatch a single function.
    candidates = ['https://' + domain + '/favicon.ico']
    try:
        html_icon = _favicon_from_html(domain)
    except Exception:
        html_icon = None
    if html_icon:
        candidates.insert(0, html_icon)
    candidates.append('https://www.google.com/s2/favicons?sz=64&domain=' + domain)
    for url in candidates:
        try:
            content, ctype = _favicon_http_get(url)
        except Exception:
            continue
        if not content or len(content) > _FAVICON_MAX_BYTES:
            continue
        if ctype and not (ctype.startswith('image/') or ctype == 'application/octet-stream'):
            continue
        return content
    return None


def _download_favicon(domain, out_path):
    content = _fetch_favicon(domain)
    if not content:
        return False
    try:
        os.makedirs(_favicons_dir(), exist_ok=True)
        tmp = out_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, out_path)
        return True
    except Exception:
        return False


# Image endpoints (thumb/avatar/favicon) let the browser cache so console polls
# don't refetch every avatar/favicon/thumbnail. Thumbnails are content-addressed
# (cache key includes mtime) and their URLs carry a ?v= version param, so they're
# safe to mark immutable for a year; avatars/favicons reuse a stable URL and only
# get a short TTL. 404 fallbacks return JSON via _json (no caching) so the onerror
# cascade keeps working.
def _send_image(path, mimetype, cache_control):
    resp = send_file(path, mimetype=mimetype)
    resp.headers['Cache-Control'] = cache_control
    return resp


_THUMB_CACHE_CONTROL = 'private, max-age=31536000, immutable'
_AVATAR_CACHE_CONTROL = 'private, max-age=3600'
_FAVICON_CACHE_CONTROL = 'private, max-age=86400'


# The list endpoint serves aggregates straight from the index, so externally
# deleted files (video_filter, temp repair, manual rm) would keep inflating
# videoCount/totalSize until the next scan or a detail request. Re-validate the
# indexed rows against the disk at most once per interval: the list endpoint is
# polled frequently, and an unthrottled per-file stat on every poll would undo
# the point of the aggregate query.
_LIST_VALIDATE_INTERVAL = 30  # seconds
_list_validate_lock = threading.Lock()
_list_validated_ts = 0.0


def _validate_indexed_files(streamers):
    # Drop index rows whose file no longer exists (same isfile + mark_deleted
    # sync as the detail endpoint). Returns True when anything was marked
    # deleted so the caller can re-fetch the aggregate. Throttled; no-op inside
    # the interval.
    global _list_validated_ts
    with _list_validate_lock:
        now = time()
        if now - _list_validated_ts < _LIST_VALIDATE_INTERVAL:
            return False
        _list_validated_ts = now
    changed = False
    for s in streamers:
        rows = media_index.list_videos(s.siteslug, s.username)
        for row in rows or ():
            if not os.path.isfile(row['abs_path']):
                media_index.mark_deleted(row['abs_path'])
                changed = True
    return changed


def _recycle_root():
    # Recycle-bin root used for the videos' inRecycle flag. Prefer the
    # parameters getter once the recycle-bin engine lands it; until then follow
    # the agreed convention: '_recycle' resolved against the download dir.
    getter = (getattr(parameters, 'get_nsfw_recycle_dir', None)
              or getattr(parameters, 'get_recycle_dir', None))
    if callable(getter):
        try:
            return os.path.abspath(getter())
        except Exception:
            pass
    d = (getattr(parameters, 'NSFW_RECYCLE_DIR', None)
         or getattr(parameters, 'RECYCLE_DIR', None) or '_recycle')
    if not os.path.isabs(d):
        d = os.path.join(parameters.get_download_dir(), d)
    return os.path.abspath(d)


def _classification_map(siteslug, username):
    # {filename: classification} for one streamer in a single SQLite query (no
    # per-file disk IO). Only used while media_index.list_videos rows do not
    # carry a classification key themselves; any miss returns an empty dict so
    # the API reports null (not yet classified / index unavailable).
    try:
        if not media_index._index_readable():
            return {}
        with closing(media_index.connect()) as conn:
            rows = conn.execute(
                'SELECT filename, classification FROM media_files '
                'WHERE deleted=0 AND site=? AND username=?',
                (siteslug, username)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _recording_disk_entry(streamer):
    # (filename, abs_path, size, mtime) of the file the recorder is writing,
    # stat'ed from disk, or None. The scanner deliberately never indexes this
    # file, so index-served responses must merge it back in to match the
    # disk-scan path (which does include it).
    if not getattr(streamer, 'recording', False):
        return None
    recording_name = getattr(streamer, 'recording_filename', None)
    if not recording_name:
        return None
    rec_path = os.path.join(streamer.outputFolder, recording_name)
    try:
        rec_stat = os.stat(rec_path)
    except OSError:
        return None
    return (recording_name, os.path.abspath(rec_path),
            rec_stat.st_size, int(rec_stat.st_mtime))


def register_api_v2(app, manager, login_required):
    @app.route('/api/v2/streamers', methods=['GET'])
    @login_required
    def api_v2_streamers():
        # One aggregate SQLite query covers every streamer; entries missing from
        # the index (or a missing/disabled index) fall back per-streamer to the
        # cached video_files enumeration inside _streamer_summary.
        stats = media_index.get_stats_by_streamer() or {}
        if stats and _validate_indexed_files(manager.streamers):
            stats = media_index.get_stats_by_streamer() or {}
        payload = []
        for s in manager.streamers:
            s_stats = stats.get((s.siteslug, s.username))
            if s_stats is not None:
                # The index normally lacks the file being recorded; merge it
                # from disk so videoCount/totalSize/lastRecordedTs match the
                # cache-based path (which does include it). Skip the merge if
                # the scanner raced the recording start and already indexed it
                # (same dedup as the detail endpoint), or it would be counted
                # twice. Streamers with no indexed rows keep the
                # cached-enumeration fallback.
                rec = _recording_disk_entry(s)
                if rec is not None:
                    rec_name, _path, rec_size, rec_mtime = rec
                    if media_index.get_video(s.siteslug, s.username, rec_name) is None:
                        s_stats = {
                            'count': s_stats['count'] + 1,
                            'total_size': s_stats['total_size'] + rec_size,
                            'last_mtime': max(s_stats['last_mtime'] or 0, rec_mtime),
                        }
            payload.append(_streamer_summary(s, s_stats))
        return _json(payload)

    @app.route('/api/v2/streamers/<site>/<user>', methods=['GET'])
    @login_required
    def api_v2_streamer_detail(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        recording_name = getattr(streamer, 'recording_filename', None)
        # Prefer the media index (one SQLite query, durations included) over
        # re-scanning the directory. On any miss fall back to the existing
        # disk scan so behavior is unchanged when the index is off or cold.
        indexed = media_index.list_videos(streamer.siteslug, streamer.username)
        if indexed is not None:
            # Classification comes from the index only (never a per-file probe):
            # use a classification key on the rows when present, otherwise fetch
            # the whole streamer's labels in one extra query.
            cls_map = None
            if not (indexed and 'classification' in indexed[0]):
                cls_map = _classification_map(streamer.siteslug, streamer.username)
            # Externally deleted files (video_filter, temp repair, purge, manual
            # rm) are not reflected until the next scan; drop stale rows here
            # and sync the index so playback never 404s on a listed video.
            entries = []
            for row in indexed:
                if os.path.isfile(row['abs_path']):
                    cls = (row['classification'] if cls_map is None
                           else cls_map.get(row['filename']))
                    entries.append((row['filename'], row['abs_path'], row['size'],
                                    row['mtime'], row['duration'], cls))
                else:
                    media_index.mark_deleted(row['abs_path'])
            # The scanner skips the file the recorder is writing, so add it
            # from disk to keep the detail response identical to the scan path.
            if recording_name not in {e[0] for e in entries}:
                rec = _recording_disk_entry(streamer)
                if rec is not None:
                    entries.append(rec + (None, None))
        else:
            streamer.cache_file_list()
            entries = [(v.filename, v.abs_path, v.filesize,
                        int(v._stat.st_mtime), None, None) for v in streamer.video_files]
        recycle_prefix = _recycle_root() + os.sep
        videos = []
        for filename, abs_path, size, mtime, indexed_duration, classification in entries:
            is_recording = bool(streamer.recording and filename == recording_name)
            # Never block the response on ffprobe: use the index's duration or
            # the warm cache only, and queue uncached files for the background
            # worker (null until probed). The file the recorder is still
            # writing is skipped entirely.
            if is_recording:
                duration = None
            else:
                duration = indexed_duration
                if duration is None:
                    duration = _get_duration_cache_only(abs_path, mtime)
                if duration is None:
                    enqueue_duration_probe(abs_path, mtime)
                # Warm the thumbnail in the background (concurrency-capped) so it
                # is ready before the browser requests it. No-op once cached.
                enqueue_thumb_prewarm(
                    abs_path, _thumb_cache_path(abs_path, mtime), mtime)
            videos.append({
                'filename': filename,
                'size': size,
                'mtime': mtime,
                'duration': duration,
                'url': f'/video/{streamer.username}/{streamer.siteslug}/{filename}',
                'recording': is_recording,
                'classification': classification,
                'inRecycle': os.path.abspath(abs_path).startswith(recycle_prefix),
            })
        videos.sort(key=lambda v: v['mtime'], reverse=True)
        # Opportunistically drop durations.json entries for files that no longer
        # exist. Gated by size/growth so it stays off the common request path.
        _maybe_cleanup_durations(manager.streamers)
        # Keep the summary counters consistent with the videos we just listed
        # when they came from the index (the fallback path just re-reads the
        # freshly refreshed video_files cache).
        index_stats = None
        if indexed is not None:
            index_stats = {
                'count': len(videos),
                'total_size': sum(v['size'] for v in videos),
                'last_mtime': max((v['mtime'] for v in videos), default=None),
            }
        detail = _streamer_summary(streamer, index_stats)
        detail['videos'] = videos
        return _json(detail)

    @app.route('/api/v2/streamers/<site>/<user>/schedule', methods=['GET'])
    @login_required
    def api_v2_streamer_schedule(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # Backfill historical online hours from existing recordings (once per
        # streamer per process): file mtime is the session end, duration comes from
        # the warm durations cache (uncached files still contribute their end hour).
        try:
            streamer.cache_file_list()
            sessions = [
                (video._stat.st_mtime,
                 _get_duration_cache_only(video.abs_path, int(video._stat.st_mtime)))
                for video in streamer.video_files
            ]
            online_history.backfill(streamer.site, streamer.username, sessions)
        except Exception:
            pass
        return _json(online_history.compute_schedule(streamer.site, streamer.username))

    @app.route('/api/v2/streamers/<site>/<user>/live', methods=['GET'])
    @login_required
    def api_v2_streamer_live(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        if not getattr(streamer, 'recording', False):
            return _json({'error': 'Not recording'}, 404)
        tmp_path = _live_tmp_path(streamer)
        if not tmp_path or not os.path.exists(tmp_path):
            return _json({'error': 'Live stream not available'}, 404)

        # Detect the container actually on disk. StripChat/CMAF writes fragmented
        # MP4 (ftyp/moov + moof/mdat) into the .tmp.ts; other sites write raw
        # MPEG-TS. Serve each with the correct mimetype and start boundary.
        try:
            with open(tmp_path, 'rb') as f:
                size = os.fstat(f.fileno()).st_size
                header = f.read(12)
                is_fmp4 = header[4:8] == b'ftyp'
                init_bytes = b''
                if is_fmp4:
                    mimetype = 'video/mp4'
                    init_end = _fmp4_init_end(f)
                    if init_end:
                        f.seek(0)
                        init_bytes = f.read(init_end)
                    win_base = max(init_end or 0, size - _LIVE_TAIL_BYTES)
                    f.seek(win_base)
                    window = f.read()
                    start = _fmp4_last_moof(window, win_base)
                    if start is None:
                        start = init_end or 0
                else:
                    mimetype = 'video/mp2t'
                    start = max(0, size - _LIVE_TAIL_BYTES)
                    start -= start % _TS_PACKET_SIZE  # align to a TS packet
        except OSError:
            return _json({'error': 'Live stream not available'}, 404)

        def generate():
            try:
                if init_bytes:
                    yield init_bytes
                with open(tmp_path, 'rb') as f:
                    f.seek(start)
                    idle = 0
                    while True:
                        chunk = f.read(_LIVE_READ_CHUNK)
                        if chunk:
                            idle = 0
                            yield chunk
                            continue
                        # Caught up to the write head: stop if recording ended or
                        # the temp file vanished (post-processing removed it).
                        if not getattr(streamer, 'recording', False):
                            break
                        if not os.path.exists(tmp_path):
                            break
                        idle += 1
                        if idle >= _LIVE_MAX_IDLE_ITERS:
                            break
                        sleep(_LIVE_POLL_INTERVAL)
            except (GeneratorExit, OSError):
                return

        headers = {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
        return Response(generate(), mimetype=mimetype, headers=headers)

    @app.route('/api/v2/streamers/<site>/<user>/videos/<path:filename>', methods=['DELETE'])
    @login_required
    def api_v2_delete_video(site, user, filename):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # Second line of defense behind cache_file_list's exclusion: a .tmp.
        # name is a downloader's in-flight file, never deletable via the API.
        if '.tmp.' in filename:
            return _json({'error': 'Cannot delete an in-flight temp file'}, 403)
        # Double-safety with the frontend graying-out: never delete the file
        # that is currently being written by the recorder.
        if streamer.recording and filename == getattr(streamer, 'recording_filename', None):
            return _json({'error': 'Cannot delete a recording in progress'}, 409)
        streamer.cache_file_list()
        match = next((v for v in streamer.video_files if v.filename == filename), None)
        if match is None:
            return _json({'error': 'Video not found'}, 404)
        try:
            os.remove(match.abs_path)
        except Exception:
            return _json({'error': 'Failed to delete video'}, 500)
        media_index.mark_deleted(match.abs_path)
        streamer.cache_file_list()
        return _json({'ok': True})

    @app.route('/api/v2/streamers/<site>/<user>/videos', methods=['DELETE'])
    @login_required
    def api_v2_delete_all_videos(site, user):
        # Delete every recorded video file for this streamer, keeping the entry
        # and its monitor state intact. The file currently being written by the
        # recorder is skipped (never interrupt an in-progress recording).
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        streamer.cache_file_list()
        recording_name = getattr(streamer, 'recording_filename', None)
        deleted = 0
        skipped = 0
        freed = 0
        for video in list(streamer.video_files):
            # cache_file_list already excludes '.tmp.' names; keep the guard
            # here too so a stale list can never delete an in-flight file.
            if '.tmp.' in video.filename:
                skipped += 1
                continue
            if streamer.recording and video.filename == recording_name:
                skipped += 1
                continue
            try:
                size = video.filesize
                os.remove(video.abs_path)
                media_index.mark_deleted(video.abs_path)
                deleted += 1
                freed += size
            except Exception:
                skipped += 1
        streamer.cache_file_list()
        return _json({'ok': True, 'deleted': deleted, 'skipped': skipped, 'freed': freed})

    @app.route('/api/v2/videos/classification', methods=['POST'])
    @login_required
    def api_v2_set_video_classification():
        # Manual re-labeling. The file is addressed either by its absolute
        # path or by site/user/filename (what the video list carries); either
        # way it must resolve to an indexed row, so an unknown or crafted path
        # is a 404 and can never touch a file outside the index. Writes
        # classification_source='manual', which no automatic path overwrites,
        # and restores recycled files whose new label is not 'non-adult'.
        data = request.get_json(silent=True) or {}
        label = data.get('label')
        if label not in media_index.CLASSIFICATION_LABELS:
            return _json({'error': 'Invalid label', 'code': 'label'}, 400)
        path = data.get('path')
        if not path:
            site = data.get('site')
            user = data.get('user')
            filename = data.get('filename')
            if not (site and user and filename):
                return _json({'error': 'Missing path', 'code': 'path'}, 400)
            row = media_index.get_video(site, user, filename)
            if row is None:
                return _json({'error': 'Video not found'}, 404)
            path = row['abs_path']
        if not isinstance(path, str):
            return _json({'error': 'Missing path', 'code': 'path'}, 400)
        result = media_index.reclassify_manual(path, label)
        if 'error' in result:
            return _json({'error': result['error']}, result.get('status', 400))
        return _json({'ok': True, 'restored': result['restored'],
                      'classification': label})

    @app.route('/api/v2/streamers/<site>/<user>/videos/<path:filename>/thumb', methods=['GET'])
    @login_required
    def api_v2_video_thumb(site, user, filename):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # No thumbnail for the file the recorder is still writing.
        if streamer.recording and filename == getattr(streamer, 'recording_filename', None):
            return _json({'error': 'Recording in progress'}, 404)
        # Single-row index lookup avoids re-scanning the whole folder per thumb
        # request; a stale row (file gone) or any miss falls back to the scan.
        abs_path = mtime = None
        indexed = media_index.get_video(streamer.siteslug, streamer.username, filename)
        if indexed is not None and os.path.isfile(indexed['abs_path']):
            abs_path = indexed['abs_path']
            mtime = int(indexed['mtime'])
        else:
            streamer.cache_file_list()
            match = next((v for v in streamer.video_files if v.filename == filename), None)
            if match is None:
                return _json({'error': 'Video not found'}, 404)
            abs_path = match.abs_path
            mtime = int(match._stat.st_mtime)
        thumb_path = _thumb_cache_path(abs_path, mtime)
        # Concurrency-capped generation (shared with background prewarm); the
        # cached path returns immediately without touching the semaphore.
        if not _generate_thumb_limited(abs_path, thumb_path, mtime):
            # Broken/unparseable file: 404 so the frontend falls back to its
            # generated placeholder image.
            return _json({'error': 'Thumbnail unavailable'}, 404)
        return _send_image(thumb_path, 'image/jpeg', _THUMB_CACHE_CONTROL)

    @app.route('/api/v2/streamers/<site>/<user>/avatar', methods=['GET'])
    @login_required
    def api_v2_streamer_avatar(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        cache_path = _avatar_cache_path(streamer.site, streamer.username)
        cached = os.path.exists(cache_path)
        fresh = cached and (time() - os.path.getmtime(cache_path) < _AVATAR_TTL)
        if fresh:
            return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)
        url = _avatar_source_url(streamer)
        if url and _download_avatar(streamer, url, cache_path):
            return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)
        if cached:  # stale but present, and no live source right now — serve it
            return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)
        # No extractor / offline with no prior cache / download failed: 404 so the
        # frontend falls back to its generated placeholder avatar.
        return _json({'error': 'Avatar unavailable'}, 404)

    @app.route('/api/v2/sites/<site>/favicon', methods=['GET'])
    @login_required
    def api_v2_site_favicon(site):
        site_cls = Bot.str2site(site)
        if site_cls is None:
            return _json({'error': 'Unknown site'}, 404)
        cache_path = _favicon_cache_path(site_cls.siteslug)
        cached = os.path.exists(cache_path)
        fresh = cached and (time() - os.path.getmtime(cache_path) < _FAVICON_TTL)
        if fresh:
            return _send_image(cache_path, _favicon_mime(cache_path), _FAVICON_CACHE_CONTROL)
        domain = _site_domain(manager, site_cls)
        if domain and _download_favicon(domain, cache_path):
            return _send_image(cache_path, _favicon_mime(cache_path), _FAVICON_CACHE_CONTROL)
        if cached:  # stale but present, and no fresh fetch right now — serve it
            return _send_image(cache_path, _favicon_mime(cache_path), _FAVICON_CACHE_CONTROL)
        # Unknown domain / fetch failed with no prior cache: 404 so the frontend
        # falls back to its colored dot + abbreviation.
        return _json({'error': 'Favicon unavailable'}, 404)

    @app.route('/api/v2/basesettings', methods=['GET'])
    @login_required
    def api_v2_basesettings():
        sites = {site.siteslug: site.site for site in LOADED_SITES}
        statuses = {status.value: Bot.status_messages[status] for status in Status}
        usage = OOSDetector.space_usage()
        return _json({
            'sites': sites,
            'status': statuses,
            'webListFrequency': WEB_LIST_FREQUENCY,
            # Lets the frontend show the logout entry only when a password is set.
            'authEnabled': parameters.get_webserver_password() != "",
            'diskSpace': {
                'total': usage.total,
                'free': usage.free,
                'used': usage.total - usage.free,
                'percentageFree': round(usage.free / usage.total * 100, 3),
                'freeHuman': human_file_size(usage.free),
                'totalHuman': human_file_size(usage.total),
            },
        })

    @app.route('/api/v2/streamers', methods=['POST'])
    @login_required
    def api_v2_add_streamer():
        data = request.get_json(silent=True) or {}
        username = data.get('username', '')
        site = data.get('site', '')
        if not username or not site:
            return _json({'error': 'Missing username or site'}, 400)
        existing = manager.getStreamer(username, site)
        res = manager.do_add(existing, username, site)
        if res.startswith('Added'):
            streamer = manager.getStreamer(username, site)
            return _json(_streamer_summary(streamer), 201)
        if res == 'Streamer already exists':
            return _json({'error': res}, 409)
        return _json({'error': res}, 400)

    _SINGLE_ACTIONS = {
        'pause': 'do_pause',
        'resume': 'do_resume',
        'stop-recording': 'do_stop_recording',
        'reactivate': 'do_reactivate',
    }

    @app.route('/api/v2/streamers/<site>/<user>/<action>', methods=['POST'])
    @login_required
    def api_v2_streamer_action(site, user, action):
        method = _SINGLE_ACTIONS.get(action)
        if method is None:
            return _json({'error': 'Unknown action'}, 404)
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        res = getattr(manager, method)(streamer, user, site)
        if res != 'OK':
            return _json({'error': res}, 500)
        return _json(_streamer_summary(streamer))

    @app.route('/api/v2/streamers/<site>/<user>', methods=['DELETE'])
    @login_required
    def api_v2_delete_streamer(site, user):
        data = request.get_json(silent=True) or {}
        delete_files = bool(data.get('deleteFiles', False))
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        res = manager.do_purge(streamer, user, site, delete_files=delete_files)
        if res != 'OK':
            return _json({'error': res}, 500)
        return _json({'ok': True})

    _BATCH_ACTIONS = {
        'pause': 'do_pause',
        'resume': 'do_resume',
        'stopRecording': 'do_stop_recording',
        'purge': 'do_purge',
    }

    @app.route('/api/v2/batch', methods=['POST'])
    @login_required
    def api_v2_batch():
        data = request.get_json(silent=True) or {}
        action = data.get('action')
        method = _BATCH_ACTIONS.get(action)
        if method is None:
            return _json({'error': 'Unknown action'}, 400)
        delete_files = bool(data.get('deleteFiles', False))
        results = []
        for target in data.get('targets', []):
            user = target.get('user')
            site = target.get('site')
            streamer = manager.getStreamer(user, site)
            if streamer is None:
                results.append({'site': site, 'user': user, 'ok': False, 'message': 'Streamer not found'})
                continue
            if action == 'purge':
                res = manager.do_purge(streamer, user, site, delete_files=delete_files)
            else:
                res = getattr(manager, method)(streamer, user, site)
            results.append({'site': site, 'user': user, 'ok': res == 'OK', 'message': res})
        return _json({'results': results})

    @app.route('/api/v2/history', methods=['GET'])
    @login_required
    def api_v2_history():
        return _json(config.load_history())

    @app.route('/api/v2/history/<int:idx>/readd', methods=['POST'])
    @login_required
    def api_v2_history_readd(idx):
        history = config.load_history()
        if idx < 0 or idx >= len(history):
            return _json({'error': 'History entry not found'}, 404)
        entry = history[idx]
        username = entry.get('username', '')
        site = entry.get('site', '')
        existing = manager.getStreamer(username, site)
        res = manager.do_add(existing, username, site)
        if not res.startswith('Added') and res != 'Streamer already exists':
            return _json({'error': res}, 400)
        history.pop(idx)
        config.save_history(history)
        streamer = manager.getStreamer(username, site)
        if streamer is None:
            return _json({'ok': True})
        return _json(_streamer_summary(streamer), 201)

    @app.route('/api/v2/history/<int:idx>', methods=['DELETE'])
    @login_required
    def api_v2_history_delete(idx):
        history = config.load_history()
        if idx < 0 or idx >= len(history):
            return _json({'error': 'History entry not found'}, 404)
        history.pop(idx)
        config.save_history(history)
        return _json({'ok': True})

    @app.route('/api/v2/history/<int:idx>/avatar', methods=['GET'])
    @login_required
    def api_v2_history_avatar(idx):
        # Purged streamers leave their cached avatar in .thumbs/avatars/ (do_purge
        # only removes the streamer's own folder), so the history page can still
        # show the real face. There is no live bot to refetch through, so we only
        # serve an existing cache; anything missing 404s and the frontend falls
        # back to its generated placeholder.
        history = config.load_history()
        if idx < 0 or idx >= len(history):
            return _json({'error': 'History entry not found'}, 404)
        entry = history[idx]
        cache_path = _avatar_cache_path(entry.get('site', ''), entry.get('username', ''))
        if not os.path.exists(cache_path):
            return _json({'error': 'Avatar unavailable'}, 404)
        return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)

    @app.route('/api/v2/settings/download-dir', methods=['GET'])
    @login_required
    def api_v2_get_download_dir():
        return _json(_download_dir_payload())

    @app.route('/api/v2/settings/download-dir', methods=['PUT', 'POST'])
    @login_required
    def api_v2_set_download_dir():
        data = request.get_json(silent=True) or {}
        candidate, err = _validate_download_dir(data.get('path', ''))
        if err:
            code, message = err
            return _json({'error': message, 'code': code}, 400)
        # New recordings pick up the change immediately (bot.py reads the dir at
        # write time); existing files in the old directory are not migrated.
        parameters.set_download_dir(candidate)
        parameters.persist_download_dir(candidate)
        return _json(_download_dir_payload())

    @app.route('/api/v2/settings/min-video-size', methods=['GET'])
    @login_required
    def api_v2_get_min_video_size():
        return _json({'minVideoSizeMb': parameters.get_min_video_size_mb()})

    @app.route('/api/v2/settings/min-video-size', methods=['PUT', 'POST'])
    @login_required
    def api_v2_set_min_video_size():
        data = request.get_json(silent=True) or {}
        raw = data.get('minVideoSizeMb')
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return _json({'error': 'Value must be a number', 'code': 'invalid'}, 400)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return _json({'error': 'Value must be a number', 'code': 'invalid'}, 400)
        if not math.isfinite(value) or value < 0 or value > 10240:
            return _json({'error': 'Value must be between 0 and 10240', 'code': 'invalid'}, 400)
        # Takes effect immediately: video_filter and the media index scanner
        # read the threshold via the getter. 0 disables both the
        # post-recording filter and the library scan cleanup.
        parameters.set_min_video_size_mb(value)
        parameters.persist_min_video_size_mb(value)
        return _json({'minVideoSizeMb': parameters.get_min_video_size_mb()})

    @app.route('/api/v2/settings/nsfw', methods=['GET'])
    @login_required
    def api_v2_get_nsfw():
        return _json(_nsfw_payload())

    @app.route('/api/v2/settings/nsfw', methods=['PUT', 'POST'])
    @login_required
    def api_v2_set_nsfw():
        data = request.get_json(silent=True) or {}
        updates, err = _validate_nsfw_settings(data)
        if err:
            code, message = err
            return _json({'error': message, 'code': code}, 400)
        # Takes effect immediately: start_monitor reads the flag via the getter
        # when a recording starts. Disabling releases the loaded model and
        # makes in-progress sampling threads exit without writing
        # classifications; recordings are never affected.
        enabled = updates['enabled']
        parameters.set_nsfw_enabled(enabled)
        parameters.persist_nsfw_enabled(enabled)
        if 'action' in updates:
            parameters.set_nsfw_action(updates['action'])
            parameters.persist_nsfw_action(updates['action'])
        if 'recycleRetentionDays' in updates:
            parameters.set_nsfw_recycle_retention_days(updates['recycleRetentionDays'])
            parameters.persist_nsfw_recycle_retention_days(updates['recycleRetentionDays'])
        if 'scanIntervalHours' in updates:
            parameters.set_nsfw_scan_interval_hours(updates['scanIntervalHours'])
            parameters.persist_nsfw_scan_interval_hours(updates['scanIntervalHours'])
        if 'threshold' in updates:
            parameters.set_nsfw_threshold(updates['threshold'])
            parameters.persist_nsfw_threshold(updates['threshold'])
        if not enabled:
            from streamonitor import nsfw_analyzer
            nsfw_analyzer.unload_session()
        return _json(_nsfw_payload())

    @app.route('/api/v2/settings/nsfw/backscan/progress', methods=['GET'])
    @login_required
    def api_v2_get_nsfw_backscan():
        from streamonitor import nsfw_backfill
        return _json(nsfw_backfill.progress(manager.streamers))

    @app.route('/api/v2/settings/nsfw/backscan/start', methods=['POST'])
    @login_required
    def api_v2_start_nsfw_backscan():
        # Labels every unanalyzed indexed video serially in one background
        # thread. Refused while the NSFW feature is off; an already running
        # session is never restarted, its current state is returned instead.
        from streamonitor import nsfw_backfill
        if not parameters.get_nsfw_enabled():
            return _json({'error': 'NSFW analysis is disabled', 'code': 'disabled'}, 409)
        state, started = nsfw_backfill.start(manager.streamers)
        state['started'] = started
        return _json(state)

    @app.route('/api/v2/settings/nsfw/backscan/cancel', methods=['POST'])
    @login_required
    def api_v2_cancel_nsfw_backscan():
        # Stops after the file being analyzed finishes; a no-op when idle.
        from streamonitor import nsfw_backfill
        return _json(nsfw_backfill.cancel(manager.streamers))

    @app.route('/api/v2/settings/recording-resolution', methods=['GET'])
    @login_required
    def api_v2_get_recording_resolution():
        return _json(_recording_resolution_payload())

    @app.route('/api/v2/settings/recording-resolution', methods=['PUT', 'POST'])
    @login_required
    def api_v2_set_recording_resolution():
        data = request.get_json(silent=True) or {}
        resolution, preference, err = _validate_recording_resolution(data)
        if err:
            code, message = err
            return _json({'error': message, 'code': code}, 400)
        # The next recording picks up the change immediately (bot.py reads the
        # setting at playlist-selection time); in-progress recordings are
        # unaffected.
        parameters.set_recording_resolution(resolution, preference)
        parameters.persist_recording_resolution(resolution, preference)
        return _json(_recording_resolution_payload())

    @app.route('/api/v2/settings/password', methods=['GET'])
    @login_required
    def api_v2_get_password_settings():
        # Never returns the password itself, only the user name, whether auth is
        # on and the length bounds the UI validates against.
        return _json({
            'authEnabled': parameters.get_webserver_password() != "",
            'username': parameters.get_webserver_username(),
            'minLength': parameters.WEBSERVER_PASSWORD_MIN_LENGTH,
            'maxLength': parameters.WEBSERVER_PASSWORD_MAX_LENGTH,
            'usernameMinLength': parameters.WEBSERVER_USERNAME_MIN_LENGTH,
            'usernameMaxLength': parameters.WEBSERVER_USERNAME_MAX_LENGTH,
        })

    @app.route('/api/v2/settings/password', methods=['PUT', 'POST'])
    @login_required
    def api_v2_set_password():
        current = parameters.get_webserver_password()
        if current == "":
            return _json({'error': 'Password auth is disabled', 'code': 'disabled'}, 409)
        data = request.get_json(silent=True) or {}
        supplied = data.get('currentPassword')
        new_password = data.get('newPassword')
        new_username = data.get('newUsername')
        # The user name is optional: omit it to only change the password, send
        # it without a newPassword to only rename the account.
        changing_username = new_username is not None
        if changing_username:
            if not isinstance(new_username, str):
                return _json({'error': 'New user name is invalid', 'code': 'invalid_username'}, 400)
            new_username = new_username.strip()
            if not (parameters.WEBSERVER_USERNAME_MIN_LENGTH <= len(new_username)
                    <= parameters.WEBSERVER_USERNAME_MAX_LENGTH):
                return _json({'error': 'New user name length is out of range',
                              'code': 'invalid_username'}, 400)
        changing_password = new_password is not None or not changing_username
        if changing_password:
            if not isinstance(new_password, str) or not new_password:
                return _json({'error': 'New password is required', 'code': 'invalid'}, 400)
            if not (parameters.WEBSERVER_PASSWORD_MIN_LENGTH <= len(new_password)
                    <= parameters.WEBSERVER_PASSWORD_MAX_LENGTH):
                return _json({'error': 'New password length is out of range', 'code': 'invalid'}, 400)
        if not isinstance(supplied, str) or not compare_digest(supplied, current):
            sleep(PASSWORD_FAILURE_DELAY)   # cheap brute-force deterrent
            return _json({'error': 'Current password is incorrect', 'code': 'wrong_password'}, 403)
        # Takes effect immediately: check_auth reads both halves via the getters
        # at call time. The current session cookie stays valid.
        if changing_username:
            parameters.set_webserver_username(new_username)
            parameters.persist_webserver_username(new_username)
        if changing_password:
            parameters.set_webserver_password(new_password)
            parameters.persist_webserver_password(new_password)
        return _json({'ok': True, 'authEnabled': True,
                      'username': parameters.get_webserver_username()})
