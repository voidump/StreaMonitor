import json

from flask import Response

from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.enums import Status
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


def _streamer_summary(streamer):
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
        'recordingBytes': getattr(streamer, 'recording_bytes', 0),
        'videoCount': len(streamer.video_files),
        'totalSize': streamer.video_files_total_size,
        'lastCheckTs': getattr(streamer, 'lastCheckTs', None),
    }


def _json(payload, status_code=200):
    return Response(json.dumps(payload), status=status_code, mimetype='application/json')


def register_api_v2(app, manager, login_required):
    @app.route('/api/v2/streamers', methods=['GET'])
    @login_required
    def api_v2_streamers():
        return _json([_streamer_summary(s) for s in manager.streamers])

    @app.route('/api/v2/streamers/<site>/<user>', methods=['GET'])
    @login_required
    def api_v2_streamer_detail(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        streamer.cache_file_list()
        videos = []
        for video in streamer.video_files:
            videos.append({
                'filename': video.filename,
                'size': video.filesize,
                'mtime': int(video._stat.st_mtime),
                'url': f'/video/{streamer.username}/{streamer.siteslug}/{video.filename}',
            })
        videos.sort(key=lambda v: v['mtime'], reverse=True)
        detail = _streamer_summary(streamer)
        detail['videos'] = videos
        return _json(detail)

    @app.route('/api/v2/basesettings', methods=['GET'])
    @login_required
    def api_v2_basesettings():
        sites = {site.siteslug: site.site for site in LOADED_SITES}
        statuses = {status.value: Bot.status_messages[status] for status in Status}
        usage = OOSDetector.space_usage()
        return _json({
            'sites': sites,
            'status': statuses,
            'diskSpace': {
                'total': usage.total,
                'free': usage.free,
                'used': usage.total - usage.free,
                'percentageFree': round(usage.free / usage.total * 100, 3),
                'freeHuman': human_file_size(usage.free),
                'totalHuman': human_file_size(usage.total),
            },
        })
