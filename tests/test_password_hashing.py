import base64
import os
import subprocess
import sys

import pytest

import parameters
from streamonitor.managers.httpmanager import HTTPManager


def basic_auth(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.fixture
def preserve_credentials(monkeypatch):
    monkeypatch.setattr(parameters, 'WEBSERVER_PASSWORD', parameters.WEBSERVER_PASSWORD)
    monkeypatch.setattr(parameters, 'WEBSERVER_USERNAME', parameters.WEBSERVER_USERNAME)
    yield


def _client():
    return HTTPManager([]).create_app().test_client()


def test_login_against_hashed_credential(preserve_credentials):
    parameters.set_webserver_password(parameters.hash_webserver_password('pw123'))
    client = _client()
    ok = client.post('/login', json={'username': 'admin', 'password': 'pw123'})
    assert ok.status_code == 200
    bad = client.post('/login', json={'username': 'admin', 'password': 'pw124'})
    assert bad.status_code == 401


def test_legacy_plaintext_still_authenticates(preserve_credentials):
    # A hand-edited plaintext .env keeps working until the startup migration
    # rewrites it as a hash.
    parameters.set_webserver_password('legacy-plain')
    client = _client()
    assert client.post('/login', json={'username': 'admin', 'password': 'legacy-plain'}).status_code == 200


def test_password_change_revokes_other_sessions(tmp_path, monkeypatch, preserve_credentials):
    monkeypatch.chdir(tmp_path)
    app = HTTPManager([]).create_app()
    changer = app.test_client()
    bystander = app.test_client()
    assert changer.post('/login', json={'username': 'admin', 'password': 'admin'}).status_code == 200
    assert bystander.post('/login', json={'username': 'admin', 'password': 'admin'}).status_code == 200

    response = changer.put('/api/v2/settings/password',
                           json={'currentPassword': 'admin', 'newPassword': 'brand-new'})
    assert response.status_code == 200
    # The requester keeps their session; every other session is revoked.
    assert changer.get('/api/v2/streamers').status_code == 200
    assert bystander.get('/api/v2/streamers').status_code == 401
    # And the new password logs in while the old one does not.
    fresh = app.test_client()
    assert fresh.post('/login', json={'username': 'admin', 'password': 'admin'}).status_code == 401
    assert fresh.post('/login', json={'username': 'admin', 'password': 'brand-new'}).status_code == 200


def test_hash_survives_env_roundtrip(tmp_path):
    # The werkzeug hash contains '$' and ':'; it must round-trip through the
    # .env quoting and django-environ's parser byte-for-byte.
    hashed = parameters.hash_webserver_password('secret')
    probe = (
        "import parameters\n"
        "parameters.persist_webserver_password(%r)\n"
        "import importlib, os\n"
        "import environ\n"
        "env2 = environ.Env()\n"
        "environ.Env.read_env(parameters.data_path('.env'), overwrite=True)\n"
        "print(env2.str('STRMNTR_PASSWORD'))\n" % hashed)
    result = subprocess.run(
        [sys.executable, '-c', probe],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, 'STRMNTR_DATA_DIR': str(tmp_path)},
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hashed


def test_lockout_redirect_carries_locked_error(preserve_credentials):
    import streamonitor.managers.httpmanager.httpmanager as hm
    client = _client()
    for _ in range(hm.LOGIN_MAX_FAILURES):
        client.post('/login', data={'username': 'admin', 'password': 'wrong'})
    locked = client.post('/login', data={'username': 'admin', 'password': 'admin'})
    assert locked.status_code == 302
    assert 'error=locked' in locked.headers['Location']
