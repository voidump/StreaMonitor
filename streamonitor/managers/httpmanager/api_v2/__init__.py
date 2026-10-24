"""/api/v2 — the console's JSON API, split by domain.

register_api_v2 wires every module's routes onto the Flask app. The re-exports
below are the package's public surface for the rest of the code base
(bot.py, media_index.py, httpmanager.py).
"""
from . import branding, events, history_api, live, media_cache, settings_api, streamers_api, videos_api
from .common import _json, _live_tmp_path, _recording_disk_entry, _send_image, _status_key, _streamer_summary
from .events import EVENTS_MAX_CONCURRENT
from .live import LIVE_MAX_CONCURRENT
from .media_cache import (_get_duration_cache_only, _thumb_cache_path,
                          enqueue_duration_probe, enqueue_thumb_prewarm,
                          rekey_video_caches)


__all__ = [
    'branding', 'events', 'history_api', 'live', 'media_cache', 'settings_api',
    'streamers_api', 'videos_api',
    '_json', '_live_tmp_path', '_recording_disk_entry', '_send_image',
    '_status_key', '_streamer_summary',
    'EVENTS_MAX_CONCURRENT', 'LIVE_MAX_CONCURRENT',
    '_get_duration_cache_only', '_thumb_cache_path',
    'enqueue_duration_probe', 'enqueue_thumb_prewarm', 'rekey_video_caches',
    'register_api_v2',
]


def register_api_v2(app, manager, login_required):
    streamers_api.register(app, manager, login_required)
    videos_api.register(app, manager, login_required)
    live.register(app, manager, login_required)
    events.register(app, manager, login_required)
    branding.register(app, manager, login_required)
    history_api.register(app, manager, login_required)
    settings_api.register(app, manager, login_required)
