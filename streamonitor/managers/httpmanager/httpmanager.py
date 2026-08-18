from typing import cast, Union

from datetime import timedelta
# The session proxy is aliased so it is not mistaken for a requests session
# (see tests/test_http_timeouts.py, which requires an explicit timeout on every
# `session.get(...)` call).
from flask import Flask, jsonify, redirect, request, send_from_directory, session as flask_session
import os
import logging
from time import sleep

import parameters
from parameters import WEBSERVER_HOST, WEBSERVER_PORT, WEBSERVER_SKIN
import streamonitor.log as log
from functools import wraps
from secrets import compare_digest, token_hex
from streamonitor.bot import Bot
from streamonitor.manager import Manager

from .api_v2 import register_api_v2


# The Flask secret key is persisted here so session cookies (and therefore
# logins) stay valid across restarts.
SESSION_SECRET_FILE = '.session_secret'
# Sessions are permanent and slide forward on every request, so a login lasts
# as long as the browser keeps the cookie.
SESSION_LIFETIME_DAYS = 365
# Delay after a failed login attempt, a cheap brute-force deterrent.
LOGIN_FAILURE_DELAY = 1


def load_or_create_secret_key(path=SESSION_SECRET_FILE):
    # Reuse the stored key when present, otherwise generate one and write it
    # with 0600 permissions. If the file cannot be read or written we fall back
    # to an in-memory key: logins then only survive until the next restart.
    try:
        with open(path, 'r') as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(key + '\n')
    except OSError:
        pass
    return key


class HTTPManager(Manager):
    def __init__(self, streamers):
        super().__init__(streamers)
        self.logger = log.Logger("manager")

        skin = WEBSERVER_SKIN
        if skin != 'webui':
            self.logger.warning(
                f"Unknown web UI skin '{skin}'; the legacy skins were removed, falling back to 'webui'")
            skin = 'webui'
        self.skin = skin

    def create_app(self):
        app = Flask(__name__, static_folder=None)

        webui_dir = os.path.join(os.path.dirname(__file__), 'webui')

        werkzeug_logger = logging.getLogger('werkzeug')
        werkzeug_logger.disabled = True

        app.secret_key = load_or_create_secret_key()
        app.config.update(
            PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_LIFETIME_DAYS),
            # Every response re-sends the cookie, so an active user is never
            # logged out by the lifetime expiring.
            SESSION_REFRESH_EACH_REQUEST=True,
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE='Lax',
        )

        def password_enabled():
            # Read at call time so a change made through the settings API takes
            # effect without a restart.
            return parameters.get_webserver_password() != ""

        def check_auth(username, password):
            if not password_enabled():
                return True
            if not isinstance(username, str) or not isinstance(password, str):
                return False
            # Both halves go through compare_digest and both are always
            # evaluated, so a wrong user name is not distinguishable from a
            # wrong password by timing.
            user_ok = compare_digest(username, parameters.get_webserver_username())
            password_ok = compare_digest(password, parameters.get_webserver_password())
            return user_ok and password_ok

        def authenticated():
            if not password_enabled():
                return True
            if flask_session.get('authenticated') is True:
                return True
            # Basic Auth stays supported for scripts/curl, but no
            # WWW-Authenticate header is ever returned so browsers do not show
            # the native credentials prompt anymore.
            auth = request.authorization
            return bool(auth and check_auth(auth.username, auth.password))

        def login_required(f):
            @wraps(f)
            def wrapped_view(**kwargs):
                if not authenticated():
                    # Resources the frontend or a script fetches get a
                    # machine-readable 401 (never a WWW-Authenticate header, so
                    # browsers no longer show the native credentials prompt);
                    # page requests are redirected to the login page.
                    resource = (request.path.startswith('/api/')
                                or request.path.startswith('/video/'))
                    if request.method == 'GET' and not resource:
                        return redirect('/login')
                    return jsonify({'error': 'unauthorized'}), 401

                return f(**kwargs)

            return wrapped_view

        @app.route('/login', methods=['GET', 'POST'])
        def login():
            if not password_enabled():
                return redirect('/')
            if request.method == 'GET':
                if flask_session.get('authenticated') is True:
                    return redirect('/')
                return send_from_directory(webui_dir, 'login.html')
            data = request.get_json(silent=True) or {}
            password = data.get('password')
            if password is None:
                password = request.form.get('password', '')
            username = data.get('username')
            if username is None:
                username = request.form.get('username')
            if username is None:
                # Older JSON clients only send a password; keep them working by
                # assuming the configured user name.
                username = parameters.get_webserver_username()
            if check_auth(username if isinstance(username, str) else '',
                          password if isinstance(password, str) else ''):
                flask_session.permanent = True
                flask_session['authenticated'] = True
                if request.is_json:
                    return jsonify({'ok': True})
                return redirect('/')
            sleep(LOGIN_FAILURE_DELAY)
            if request.is_json:
                # Deliberately does not say which half was wrong.
                return jsonify({'error': 'invalid_credentials'}), 401
            # Plain form post: stay on the login page, the ?error flag makes it
            # show the "wrong user name or password" hint.
            return redirect('/login?error=1')

        @app.route('/logout', methods=['POST'])
        def logout():
            flask_session.clear()
            if request.is_json:
                return jsonify({'ok': True})
            return redirect('/login')

        @app.route('/', methods=['GET'])
        @login_required
        def status():
            return send_from_directory(webui_dir, 'console.html')

        @app.route('/video/<user>/<site>/<path:filename>', methods=['GET'])
        @login_required
        def get_video(user, site, filename):
            streamer = cast(Union[Bot, None], self.getStreamer(user, site))
            if streamer is None:
                return 'Streamer not found', 404
            # Videos classified non-adult may have been relocated (see
            # media_index.relocate_non_adult) or recycled (see
            # media_index.apply_nsfw_action); fall back to the streamer's
            # non-adult and recycle folders so their URLs keep playing after
            # the move. send_from_directory does the safe path join.
            output_dir = os.path.abspath(streamer.outputFolder)
            for folder in (streamer.nonAdultFolder, streamer.recycleFolder):
                folder = os.path.abspath(folder)
                if (not os.path.isfile(os.path.join(output_dir, filename))
                        and os.path.isfile(os.path.join(folder, filename))):
                    return send_from_directory(folder, filename)
            return send_from_directory(output_dir, filename)

        register_api_v2(app, self, login_required)

        @app.route('/css/<path:filename>', methods=['GET'])
        @login_required
        def webui_css(filename):
            return send_from_directory(os.path.join(webui_dir, 'css'), filename)

        @app.route('/js/<path:filename>', methods=['GET'])
        @login_required
        def webui_js(filename):
            return send_from_directory(os.path.join(webui_dir, 'js'), filename)

        return app

    def run(self):
        app = self.create_app()
        app.run(host=WEBSERVER_HOST, port=WEBSERVER_PORT)
