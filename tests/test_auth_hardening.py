import base64
import json

import pytest

import parameters
import streamonitor.managers.httpmanager.httpmanager as httpmanager_mod
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


# --- non-ASCII credentials (secrets.compare_digest str form raises TypeError) ---

def test_login_with_non_ascii_password_succeeds(preserve_credentials):
    parameters.set_webserver_password('中文密码')
    client = _client()
    ok = client.post('/login', json={'username': 'admin', 'password': '中文密码'})
    assert ok.status_code == 200


def test_login_with_non_ascii_password_wrong_guess_is_401_not_500(preserve_credentials):
    # compare_digest on str raises TypeError for non-ASCII input: every login
    # (right or wrong) used to 500, locking the user out until manual .env edit.
    parameters.set_webserver_password('中文密码')
    client = _client()
    bad = client.post('/login', json={'username': 'admin', 'password': 'wrong'})
    assert bad.status_code == 401
    bad2 = client.post('/login', json={'username': 'admin', 'password': '错的密码'})
    assert bad2.status_code == 401


def test_change_password_to_non_ascii_and_back(tmp_path, monkeypatch, preserve_credentials):
    monkeypatch.chdir(tmp_path)
    client = _client()
    r = client.put('/api/v2/settings/password',
                   json={'currentPassword': 'admin', 'newPassword': '中文密码'},
                   headers=basic_auth('admin', 'admin'))
    assert r.status_code == 200
    # The recovery path (changing it again with the non-ASCII current password)
    # must also work, otherwise the account is stuck.
    r2 = client.put('/api/v2/settings/password',
                    json={'currentPassword': '中文密码', 'newPassword': 'back2ascii'},
                    headers=basic_auth('admin', '中文密码'))
    assert r2.status_code == 200


def test_change_password_rejects_control_characters(tmp_path, monkeypatch, preserve_credentials):
    monkeypatch.chdir(tmp_path)
    client = _client()
    r = client.put('/api/v2/settings/password',
                   json={'currentPassword': 'admin',
                         'newPassword': 'abcd\nSTRMNTR_HOST=0.0.0.0'},
                   headers=basic_auth('admin', 'admin'))
    assert r.status_code == 400
    assert parameters.get_webserver_password() == 'admin'
    assert not (tmp_path / '.env').exists()


# --- per-IP login failure lockout ---

def test_basic_auth_lockout_after_repeated_failures(preserve_credentials):
    client = _client()
    wrong = basic_auth('admin', 'not-it')
    for _ in range(httpmanager_mod.LOGIN_MAX_FAILURES):
        assert client.get('/api/v2/streamers', headers=wrong).status_code == 401
    # Locked out: even the correct credentials are refused for the window.
    locked = client.get('/api/v2/streamers', headers=basic_auth('admin', 'admin'))
    assert locked.status_code == 401


def test_form_login_lockout_returns_429(preserve_credentials):
    client = _client()
    for _ in range(httpmanager_mod.LOGIN_MAX_FAILURES):
        r = client.post('/login', json={'username': 'admin', 'password': 'wrong'})
        assert r.status_code == 401
    locked = client.post('/login', json={'username': 'admin', 'password': 'admin'})
    assert locked.status_code == 429
    assert json.loads(locked.data)['error'] == 'too_many_attempts'


def test_success_resets_failure_counter(preserve_credentials):
    client = _client()
    for _ in range(httpmanager_mod.LOGIN_MAX_FAILURES - 1):
        client.post('/login', json={'username': 'admin', 'password': 'wrong'})
    ok = client.post('/login', json={'username': 'admin', 'password': 'admin'})
    assert ok.status_code == 200
    # Counter cleared: the next few failures start from zero again.
    r = client.post('/login', json={'username': 'admin', 'password': 'wrong'})
    assert r.status_code == 401


def test_lockout_expires(monkeypatch, preserve_credentials):
    monkeypatch.setattr(httpmanager_mod, 'LOGIN_LOCKOUT_SECONDS', 0.05)
    monkeypatch.setattr(httpmanager_mod, 'LOGIN_LOCKOUT_MAX_SECONDS', 0.05)
    client = _client()
    for _ in range(httpmanager_mod.LOGIN_MAX_FAILURES):
        client.post('/login', json={'username': 'admin', 'password': 'wrong'})
    import time
    time.sleep(0.1)
    ok = client.post('/login', json={'username': 'admin', 'password': 'admin'})
    assert ok.status_code == 200


def test_failures_are_logged_with_source_ip(preserve_credentials, caplog):
    import logging
    client = _client()
    with caplog.at_level(logging.WARNING):
        client.post('/login', json={'username': 'admin', 'password': 'wrong'})
    assert any('127.0.0.1' in rec.getMessage() for rec in caplog.records)


# --- startup credential hardening (default replacement + hash migration) ---

def test_default_password_is_replaced_with_a_hash(monkeypatch, preserve_credentials):
    persisted = []
    monkeypatch.setattr(parameters, 'persist_webserver_password',
                        lambda value: persisted.append(value))
    manager = HTTPManager([])
    generated = manager._harden_credentials()
    assert generated is not None and generated != 'admin'
    assert parameters.webserver_password_is_hashed()
    assert parameters.check_webserver_password(generated)
    assert len(persisted) == 1 and parameters.webserver_password_is_hashed(persisted[0])


def test_explicit_plaintext_password_is_migrated_to_hash(monkeypatch, preserve_credentials):
    parameters.set_webserver_password('my-own-password')
    persisted = []
    monkeypatch.setattr(parameters, 'persist_webserver_password',
                        lambda value: persisted.append(value))
    manager = HTTPManager([])
    assert manager._harden_credentials() is None  # nothing generated
    assert parameters.webserver_password_is_hashed()
    assert parameters.check_webserver_password('my-own-password')
    assert len(persisted) == 1 and parameters.webserver_password_is_hashed(persisted[0])


def test_already_hashed_password_is_left_alone(monkeypatch, preserve_credentials):
    hashed = parameters.hash_webserver_password('pw')
    parameters.set_webserver_password(hashed)
    monkeypatch.setattr(parameters, 'persist_webserver_password',
                        lambda value: pytest.fail('must not persist'))
    manager = HTTPManager([])
    assert manager._harden_credentials() is None
    assert parameters.get_webserver_password() == hashed


def test_disabled_auth_is_left_alone(monkeypatch, preserve_credentials):
    parameters.set_webserver_password('')
    manager = HTTPManager([])
    assert manager._harden_credentials() is None
    assert parameters.get_webserver_password() == ''
