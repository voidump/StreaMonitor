from typing import cast, Union

from flask import Flask, request, send_from_directory
import os
import logging

from parameters import WEBSERVER_HOST, WEBSERVER_PORT, WEBSERVER_PASSWORD, WEBSERVER_SKIN
import streamonitor.log as log
from functools import wraps
from secrets import compare_digest
from streamonitor.bot import Bot
from streamonitor.manager import Manager

from .api_v2 import register_api_v2


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

        def check_auth(username, password):
            return WEBSERVER_PASSWORD == "" or (username == 'admin' and compare_digest(password, WEBSERVER_PASSWORD))

        def login_required(f):
            @wraps(f)
            def wrapped_view(**kwargs):
                auth = request.authorization
                if WEBSERVER_PASSWORD != "" and not (auth and check_auth(auth.username, auth.password)):
                    return ('Unauthorized', 401, {
                        'WWW-Authenticate': 'Basic realm="Login Required"'
                    })

                return f(**kwargs)

            return wrapped_view

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
            return send_from_directory(
                os.path.abspath(streamer.outputFolder),
                filename
            )

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
