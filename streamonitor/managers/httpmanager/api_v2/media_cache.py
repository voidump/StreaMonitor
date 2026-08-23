"""Video media cache: probed durations (consolidated durations.json +
background probe worker) and generated thumbnails (concurrency-capped
generation + background prewarm). All ffprobe/ffmpeg invocations for the web
UI are centralized here."""
import hashlib
import json
import os
import queue
import subprocess
import threading

import parameters
from parameters import FFMPEG_PATH
from streamonitor.utils.atomic_file import atomic_write_json

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
    # Caller holds _durations_lock. Atomic write (unique temp + fsync + rename)
    # so a crash mid-write never leaves a truncated durations.json behind.
    try:
        os.makedirs(_thumbs_dir(), exist_ok=True)
        atomic_write_json(path, _durations_cache, indent=None)
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
