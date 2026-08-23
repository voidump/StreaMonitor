"""Settings endpoints: base settings, download dir, min video size, NSFW
pipeline, recording resolution and the login credentials."""
import importlib.util
import math
import os
from time import sleep

from flask import request, session as flask_session

import parameters
from parameters import WEB_LIST_FREQUENCY
from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.enums import Status
from streamonitor.managers.outofspace_detector import OOSDetector
from streamonitor.utils import human_file_size

from .common import _json

# Delay after a change-password attempt with the wrong current password, the
# same cheap brute-force deterrent the login route uses. Module-level so tests
# can shrink it.
PASSWORD_FAILURE_DELAY = 1


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
    # Line breaks are technically legal in a directory name but the persist
    # layer rejects them (they could inject .env lines); refuse them up front
    # so the in-memory setting and .env can never diverge on a 500.
    if any(c in raw for c in ('\n', '\r', '\0')):
        return None, ('invalid', 'Path contains invalid characters')
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


def register(app, manager, login_required):
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
            if any(c in new_username for c in ('\n', '\r', '\0')):
                return _json({'error': 'New user name contains invalid characters',
                              'code': 'invalid_username'}, 400)
        changing_password = new_password is not None or not changing_username
        if changing_password:
            if not isinstance(new_password, str) or not new_password:
                return _json({'error': 'New password is required', 'code': 'invalid'}, 400)
            if not (parameters.WEBSERVER_PASSWORD_MIN_LENGTH <= len(new_password)
                    <= parameters.WEBSERVER_PASSWORD_MAX_LENGTH):
                return _json({'error': 'New password length is out of range', 'code': 'invalid'}, 400)
            # Line breaks would let a password inject arbitrary .env lines on
            # the next parse (the persist layer also rejects them).
            if any(c in new_password for c in ('\n', '\r', '\0')):
                return _json({'error': 'New password contains invalid characters',
                              'code': 'invalid'}, 400)
        # check_webserver_password handles the hashed credential and legacy
        # plaintext alike, and never raises on non-ASCII input.
        if not isinstance(supplied, str) or not parameters.check_webserver_password(supplied):
            sleep(PASSWORD_FAILURE_DELAY)   # cheap brute-force deterrent
            return _json({'error': 'Current password is incorrect', 'code': 'wrong_password'}, 403)
        # Takes effect immediately: check_auth reads both halves via the getters
        # at call time. Only a salted hash is stored/persisted.
        if changing_username:
            parameters.set_webserver_username(new_username)
            parameters.persist_webserver_username(new_username)
        if changing_password:
            parameters.set_webserver_password(
                parameters.hash_webserver_password(new_password))
            parameters.persist_webserver_password(parameters.get_webserver_password())
            # A password change revokes every session except the requester's
            # own, which is re-stamped with the new credential generation.
            if flask_session.get('authenticated') is True:
                flask_session['auth_gen'] = parameters.webserver_auth_generation()
        return _json({'ok': True, 'authEnabled': True,
                      'username': parameters.get_webserver_username()})
