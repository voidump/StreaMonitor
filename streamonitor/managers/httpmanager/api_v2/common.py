"""Shared helpers for the /api/v2 modules: JSON responses, streamer
summaries, live-recording path resolution and cached-image sending."""
import json
import os

from flask import Response, send_file

from streamonitor.enums import Status

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
