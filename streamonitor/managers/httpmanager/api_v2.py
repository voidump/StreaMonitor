import json

from flask import Response, request

import streamonitor.config as config
from parameters import WEB_LIST_FREQUENCY
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
            'webListFrequency': WEB_LIST_FREQUENCY,
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
