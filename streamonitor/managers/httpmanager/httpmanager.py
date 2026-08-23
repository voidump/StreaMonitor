from typing import cast, Union

from datetime import timedelta
# The session proxy is aliased so it is not mistaken for a requests session
# (see tests/test_http_timeouts.py, which requires an explicit timeout on every
# `session.get(...)` call).
from flask import Flask, jsonify, redirect, request, send_from_directory, session as flask_session
import os
import logging
import threading
import time

import parameters
from parameters import WEBSERVER_HOST, WEBSERVER_PORT, WEBSERVER_SKIN
import streamonitor.log as log
from functools import wraps
from secrets import compare_digest, token_hex, token_urlsafe
from streamonitor.bot import Bot
from streamonitor.manager import Manager

from .api_v2 import register_api_v2


# The Flask secret key is persisted here so session cookies (and therefore
# logins) stay valid across restarts.
SESSION_SECRET_FILE = parameters.data_path('.session_secret')
# Sessions are permanent and slide forward on every request, so an ACTIVE user
# is never logged out; 30 days only expires cookies that stopped being used.
SESSION_LIFETIME_DAYS = 30
# Per-IP login failure lockout. A per-request sleep is no deterrent when every
# request gets its own thread (it is a cheap DoS instead), so failures are
# counted per source IP and the IP is locked out with exponential backoff.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60
LOGIN_LOCKOUT_MAX_SECONDS = 300


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
            # Both halves are always evaluated, so a wrong user name is not
            # distinguishable from a wrong password by timing. Compared as
            # bytes: compare_digest on str raises TypeError for non-ASCII.
            # The password check handles both the hashed credential (normal)
            # and legacy plaintext (migrated at startup).
            user_ok = compare_digest(
                username.encode('utf-8'),
                parameters.get_webserver_username().encode('utf-8'))
            password_ok = parameters.check_webserver_password(password)
            return user_ok and password_ok

        # Per-app (per-process) login failure state, keyed by source IP.
        login_failures = {}
        login_failures_lock = threading.Lock()

        def _client_ip():
            # Behind a reverse proxy remote_addr is the proxy for everyone and
            # the lockout would lock all users together; with the opt-in trust
            # flag the first X-Forwarded-For hop identifies the client.
            if parameters.WEBSERVER_TRUST_PROXY:
                forwarded = request.headers.get('X-Forwarded-For', '')
                if forwarded:
                    return forwarded.split(',')[0].strip() or 'unknown'
            return request.remote_addr or 'unknown'

        def _login_locked(ip):
            now = time.time()
            with login_failures_lock:
                entry = login_failures.get(ip)
                return bool(entry and entry['locked_until'] > now)

        def _clear_login_failures(ip):
            with login_failures_lock:
                login_failures.pop(ip, None)

        def _register_login_failure(ip):
            with login_failures_lock:
                now = time.time()
                # Bound the table: with the port exposed, mass scans from many
                # source addresses would otherwise grow it forever.
                if len(login_failures) > 1000:
                    for key in [k for k, e in login_failures.items()
                                if e['locked_until'] < now
                                and now - e.get('last_ts', 0) > 3600]:
                        login_failures.pop(key, None)
                entry = login_failures.setdefault(
                    ip, {'count': 0, 'locked_until': 0.0, 'last_ts': 0.0})
                entry['count'] += 1
                entry['last_ts'] = now
                locked_for = 0
                if entry['count'] >= LOGIN_MAX_FAILURES:
                    over = entry['count'] - LOGIN_MAX_FAILURES
                    locked_for = min(LOGIN_LOCKOUT_SECONDS * (2 ** over),
                                     LOGIN_LOCKOUT_MAX_SECONDS)
                    entry['locked_until'] = now + locked_for
                count = entry['count']
            if locked_for:
                self.logger.warning(
                    f'Login failed from {ip}; locked out for {locked_for:g}s '
                    f'after {count} consecutive failures')
            else:
                self.logger.warning(f'Login failed from {ip}')

        def authenticated():
            if not password_enabled():
                return True
            # A session is only valid for the credential generation it was
            # issued under: changing the password revokes every other session.
            if (flask_session.get('authenticated') is True
                    and flask_session.get('auth_gen') == parameters.webserver_auth_generation()):
                return True
            # Basic Auth stays supported for scripts/curl, but no
            # WWW-Authenticate header is ever returned so browsers do not show
            # the native credentials prompt anymore. This path counts toward
            # the same per-IP lockout as the login form — without it, /api/
            # with a Basic header was an unthrottled brute-force oracle.
            auth = request.authorization
            if not auth:
                return False
            ip = _client_ip()
            if _login_locked(ip):
                return False
            if check_auth(auth.username or '', auth.password or ''):
                _clear_login_failures(ip)
                return True
            _register_login_failure(ip)
            return False

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
            ip = _client_ip()
            if _login_locked(ip):
                if request.is_json:
                    return jsonify({'error': 'too_many_attempts'}), 429
                # Distinct error code so the login page can tell the user to
                # wait instead of claiming the password was wrong.
                return redirect('/login?error=locked')
            if check_auth(username if isinstance(username, str) else '',
                          password if isinstance(password, str) else ''):
                _clear_login_failures(ip)
                flask_session.permanent = True
                flask_session['authenticated'] = True
                flask_session['auth_gen'] = parameters.webserver_auth_generation()
                if request.is_json:
                    return jsonify({'ok': True})
                return redirect('/')
            _register_login_failure(ip)
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

    def _harden_credentials(self):
        # Two startup upgrades of the stored credential:
        # 1) The factory default admin/admin on a reachable port is an open
        #    door (the shipped docker-compose binds 0.0.0.0): replace it with
        #    a generated password and print it once.
        # 2) A legacy plaintext password (hand-edited .env, pre-hash setup) is
        #    migrated to a salted hash so .env no longer holds the secret.
        stored = parameters.get_webserver_password()
        if stored == 'admin':
            generated = token_urlsafe(9)
            parameters.set_webserver_password(
                parameters.hash_webserver_password(generated))
            persisted = True
            try:
                parameters.persist_webserver_password(
                    parameters.get_webserver_password())
            except Exception as e:
                persisted = False
                self.logger.warning(f'Could not persist the generated password: {e}')
            self.logger.warning('The factory-default web password "admin" is disabled.')
            self.logger.warning(
                f'Generated web login -> user: {parameters.get_webserver_username()}'
                f'  password: {generated}')
            if persisted:
                self.logger.warning('It is saved (hashed) to .env; change it any time in Settings.')
            else:
                self.logger.warning(
                    'It could NOT be saved (data dir not writable?): this password '
                    'is valid for this run only and a new one will be generated on '
                    'the next start.')
            return generated
        if stored and not parameters.webserver_password_is_hashed(stored):
            hashed = parameters.hash_webserver_password(stored)
            parameters.set_webserver_password(hashed)
            try:
                parameters.persist_webserver_password(hashed)
                self.logger.info('Migrated the stored web password to a salted hash.')
            except Exception as e:
                self.logger.warning(f'Could not persist the hashed password: {e}')
        return None

    def run(self):
        self._harden_credentials()
        app = self.create_app()
        try:
            from waitress import serve
        except ImportError:
            # Werkzeug's dev server is not meant for production; keep it only
            # as a fallback so a missing dependency doesn't take the UI down.
            self.logger.warning(
                'waitress is not installed; falling back to the Flask dev server')
            app.run(host=WEBSERVER_HOST, port=WEBSERVER_PORT)
            return
        self.logger.info(f'Serving web console on {WEBSERVER_HOST}:{WEBSERVER_PORT}')
        # /live streams are capped separately (api_v2.LIVE_MAX_CONCURRENT), so
        # long-lived viewers can't exhaust this pool.
        serve(app, host=WEBSERVER_HOST, port=WEBSERVER_PORT, threads=32)
