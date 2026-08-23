import json
import os
import threading
from datetime import datetime

import streamonitor.log as log
from streamonitor.utils.atomic_file import atomic_write_json


# Real online-history store (v3.6). Each streamer's online observations are kept
# at hour granularity as a sorted list of "hour epochs" (int(epoch // 3600)) in a
# single small JSON file at the project root, using the same atomic write-then-
# rename + corruption-tolerant strategy as the durations cache. Retention is a
# rolling window (>= 35 days); older hours are pruned on write.
ONLINE_HISTORY_STORE = '.strmntr_online_history.json'

_HOUR = 3600
_DAY = 86400
RETENTION_DAYS = 35
_HEAT_WEEKS = 4                    # heat / session / common-hours window
_HEAT_WINDOW = _HEAT_WEEKS * 7 * _DAY

_lock = threading.RLock()
_cache = None                     # dict key -> sorted list[int] once loaded
_cache_path = None                # which store file the cache was loaded from
_last_hour = {}                   # key -> last recorded hour epoch (hot-path dedup)
_backfilled = set()               # keys already backfilled this process
_save_warned = False              # one warning per failure streak, not per write

logger = log.Logger('online_history')


def _store_path():
    return ONLINE_HISTORY_STORE


def _key(site, user):
    return f'{site}/{user}'


def _load_locked(path):
    global _cache, _cache_path
    data = {}
    try:
        with open(path) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                if isinstance(v, list):
                    data[k] = sorted({int(h) for h in v if isinstance(h, (int, float))})
    except Exception:
        data = {}
    _cache = data
    _cache_path = path


def _save_locked(path):
    global _save_warned
    try:
        atomic_write_json(path, _cache, indent=None)
        _save_warned = False
    except Exception as e:
        # Never fatal, but never silent either: a full disk or permission
        # change would otherwise stop the history from persisting with no
        # trace. Warn once until a write succeeds again.
        if not _save_warned:
            _save_warned = True
            logger.warning(f'Failed to persist online history to {path}: {e}')


def _ensure_loaded_locked():
    path = _store_path()
    if _cache is None or _cache_path != path:
        _load_locked(path)


def record_online(site, user, ts=None):
    # Mark the current hour as "online" for a streamer. Hot-path deduped per hour
    # in memory so the bot's status loop can call this every poll without touching
    # disk more than once an hour per streamer.
    now = ts if ts is not None else datetime.now().timestamp()
    hour = int(now // _HOUR)
    key = _key(site, user)
    if _last_hour.get(key) == hour:
        return
    with _lock:
        _ensure_loaded_locked()
        hours = _cache.setdefault(key, [])
        if hour in hours:
            _last_hour[key] = hour
            return
        hours.append(hour)
        hours.sort()
        cutoff = int((now - RETENTION_DAYS * _DAY) // _HOUR)
        if hours[0] < cutoff:
            _cache[key] = [h for h in hours if h >= cutoff]
        _save_locked(_store_path())
        _last_hour[key] = hour


def _session_hours(end_ts, duration, cutoff_hour):
    end_hour = int(end_ts // _HOUR)
    if end_hour < cutoff_hour:
        return []
    if duration and duration > 0:
        start_hour = max(int((end_ts - duration) // _HOUR), cutoff_hour)
    else:
        start_hour = end_hour
    return range(start_hour, end_hour + 1)


def backfill(site, user, sessions, now=None):
    # Reconstruct historical online hours from existing recordings. `sessions` is
    # an iterable of (end_ts, duration_or_None): end_ts is the file mtime and
    # duration (seconds) comes from the warm durations cache when available. Files
    # without a cached duration still contribute their end hour (a real observation)
    # so heat is non-empty without triggering a mass ffprobe. Runs at most once per
    # streamer per process and merges into the observed data (dedup).
    key = _key(site, user)
    if key in _backfilled:
        return
    now = now if now is not None else datetime.now().timestamp()
    cutoff_hour = int((now - RETENTION_DAYS * _DAY) // _HOUR)
    add = set()
    for end_ts, duration in sessions:
        try:
            add.update(_session_hours(float(end_ts), duration, cutoff_hour))
        except (TypeError, ValueError):
            continue
    with _lock:
        _ensure_loaded_locked()
        _backfilled.add(key)
        if not add:
            return
        merged = set(_cache.get(key, [])) | add
        _cache[key] = sorted(h for h in merged if h >= cutoff_hour)
        _save_locked(_store_path())


def _empty_heat():
    return [[0] * 24 for _ in range(7)]


def _avg_session_min(window_hours):
    # Average contiguous online run in the window, in minutes. Hour granularity
    # means each run is a multiple of 60; this is the honest resolution available.
    if not window_hours:
        return None
    ordered = sorted(set(window_hours))
    runs = []
    run_len = 1
    for i in range(1, len(ordered)):
        if ordered[i] == ordered[i - 1] + 1:
            run_len += 1
        else:
            runs.append(run_len)
            run_len = 1
    runs.append(run_len)
    return int(round(sum(runs) / len(runs) * 60))


def _common_hours(window_hours):
    # Contiguous block of hours-of-day around the busiest hour whose counts clear
    # half the peak. Returns [start, end) or None when data is too thin to be real.
    if not window_hours:
        return None
    hist = [0] * 24
    for h in window_hours:
        hist[datetime.fromtimestamp(h * _HOUR).hour] += 1
    peak = max(hist)
    if peak < 2:
        return None
    threshold = max(2, (peak + 1) // 2)
    peak_hour = hist.index(peak)
    start = peak_hour
    while start - 1 >= 0 and hist[start - 1] >= threshold:
        start -= 1
    end = peak_hour
    while end + 1 <= 23 and hist[end + 1] >= threshold:
        end += 1
    return [start, end + 1]


def compute_schedule(site, user, now=None):
    now = now if now is not None else datetime.now().timestamp()
    with _lock:
        _ensure_loaded_locked()
        hours = sorted(_cache.get(_key(site, user), []))
    if not hours:
        return {
            'heat': _empty_heat(), 'observedSinceTs': None, 'sampleDays': 0,
            'days7OnlineHours': None, 'avgSessionMin': None, 'commonHours': None,
        }
    heat = _empty_heat()
    window_start_hour = int((now - _HEAT_WINDOW) // _HOUR)
    week_sets = {}
    for h in hours:
        if h < window_start_hour:
            continue
        dt = datetime.fromtimestamp(h * _HOUR)
        week = int((now - h * _HOUR) // (7 * _DAY))
        if 0 <= week < _HEAT_WEEKS:
            week_sets.setdefault((dt.weekday(), dt.hour), set()).add(week)
    for (wd, hod), weeks in week_sets.items():
        heat[wd][hod] = len(weeks)
    seven_start_hour = int((now - 7 * _DAY) // _HOUR)
    window_hours = [h for h in hours if h >= window_start_hour]
    return {
        'heat': heat,
        'observedSinceTs': int(min(hours) * _HOUR),
        'sampleDays': len({datetime.fromtimestamp(h * _HOUR).date() for h in hours}),
        'days7OnlineHours': sum(1 for h in hours if h >= seven_start_hour),
        'avgSessionMin': _avg_session_min(window_hours),
        'commonHours': _common_hours(window_hours),
    }
