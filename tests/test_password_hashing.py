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


def test_revoked_session_gets_login_page_not_redirect_loop(tmp_path, monkeypatch, preserve_credentials):
    # /login GET must apply the same generation check as every other route: a
    # session revoked by a password change used to bounce between / (needs the
    # new generation -> /login) and /login (only checked the authenticated
    # flag -> /) in an infinite redirect loop.
    monkeypatch.chdir(tmp_path)
    app = HTTPManager([]).create_app()
    changer = app.test_client()
    bystander = app.test_client()
    assert changer.post('/login', json={'username': 'admin', 'password': 'admin'}).status_code == 200
    assert bystander.post('/login', json={'username': 'admin', 'password': 'admin'}).status_code == 200
    assert changer.put('/api/v2/settings/password',
                       json={'currentPassword': 'admin', 'newPassword': 'brand-new'}).status_code == 200

    login_page = bystander.get('/login')
    assert login_page.status_code == 200, 'revoked session must reach the login page'
    root = bystander.get('/')
    assert root.status_code == 302 and root.headers['Location'].endswith('/login')
    # And logging in again works from that cleared state.
    assert bystander.post('/login', json={'username': 'admin', 'password': 'brand-new'}).status_code == 200


def test_pre_generation_session_reaches_login_page(preserve_credentials):
    # A cookie issued before auth generations existed (authenticated=True, no
    # auth_gen) must land on the login page, not loop.
    app = HTTPManager([]).create_app()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['authenticated'] = True  # legacy session: no auth_gen
    assert client.get('/login').status_code == 200


def test_env_var_plaintext_migration_is_idempotent(tmp_path, monkeypatch, preserve_credentials):
    # compose-style deployments pass STRMNTR_PASSWORD as an OS env var, which
    # re-supplies the plaintext on every boot. The migration must adopt the
    # hash a previous run persisted instead of re-hashing with a fresh salt —
    # a new salt changes the session generation and revoked every login on
    # every restart.
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])

    parameters.set_webserver_password('compose-secret')  # boot 1: env plaintext
    assert manager._harden_credentials() is None
    first_hash = parameters.get_webserver_password()
    assert parameters.webserver_password_is_hashed(first_hash)
    first_gen = parameters.webserver_auth_generation()

    parameters.set_webserver_password('compose-secret')  # boot 2: env wins again
    assert manager._harden_credentials() is None
    assert parameters.get_webserver_password() == first_hash
    assert parameters.webserver_auth_generation() == first_gen


def test_plaintext_with_hash_prefix_is_not_misdetected(preserve_credentials):
    # A hand-written password that merely STARTS with 'pbkdf2:' is plaintext;
    # prefix-only detection skipped its migration and then never matched it
    # through check_password_hash — a silent permanent lockout.
    assert parameters.webserver_password_is_hashed('pbkdf2:mypassword') is False
    parameters.set_webserver_password('pbkdf2:mypassword')
    assert parameters.check_webserver_password('pbkdf2:mypassword') is True
    # A real werkzeug hash is still detected.
    assert parameters.webserver_password_is_hashed(
        parameters.hash_webserver_password('x')) is True
