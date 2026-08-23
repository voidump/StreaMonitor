import base64
import json

import streamonitor.config as config
from streamonitor.managers.httpmanager import HTTPManager

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}


def _entry(user, ts):
    return {'site': 'FakeSite', 'siteslug': 'FS', 'username': user,
            'url': f'https://fake/{user}', 'deletedAt': ts}


def test_delete_by_identity_survives_index_shift(tmp_path, monkeypatch):
    # The client fetched the list, then a concurrent purge inserted a new entry
    # at the front: every index shifted by one. With the identity in the body,
    # the delete must still remove the entry the user actually clicked.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'history_loc', 'history.json')
    config.save_history([_entry('newest', 300), _entry('middle', 200), _entry('oldest', 100)])
    client = HTTPManager([]).create_app().test_client()

    # The client believes 'middle' is at index 0 (it fetched before the shift).
    response = client.delete('/api/v2/history/0', json=_entry('middle', 200), headers=AUTH)
    assert response.status_code == 200
    remaining = {e['username'] for e in config.load_history()}
    assert remaining == {'newest', 'oldest'}


def test_delete_unknown_identity_is_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'history_loc', 'history.json')
    config.save_history([_entry('only', 100)])
    client = HTTPManager([]).create_app().test_client()
    response = client.delete('/api/v2/history/0', json=_entry('ghost', 999), headers=AUTH)
    assert response.status_code == 404
    assert len(config.load_history()) == 1


def test_delete_without_identity_keeps_working(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'history_loc', 'history.json')
    config.save_history([_entry('a', 100), _entry('b', 200)])
    client = HTTPManager([]).create_app().test_client()
    assert client.delete('/api/v2/history/1', headers=AUTH).status_code == 200
    assert [e['username'] for e in config.load_history()] == ['a']
