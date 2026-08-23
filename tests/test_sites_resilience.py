import json

import pytest
import requests

import streamonitor.sites.xlovecam as xlovecam_mod
from streamonitor.enums import Status
from streamonitor.sites.sexchathu import SexChatHU
from streamonitor.sites.stripchat import StripChat
from streamonitor.sites.xlovecam import XLoveCam


def _fail_post(*args, **kwargs):
    raise requests.exceptions.ConnectionError('dns blip')


# --- XLoveCam: lazy, retryable performer-id resolution ---

def test_xlovecam_constructor_survives_network_failure(monkeypatch):
    # A network hiccup at startup used to propagate out of __init__, which made
    # loadStreamers drop the entry — and the next saveConfig erased it from
    # config.json for good.
    monkeypatch.setattr(xlovecam_mod.requests, 'post', _fail_post)
    bot = XLoveCam('alice')
    assert bot._id is None


def test_xlovecam_lookup_failure_is_unknown_not_notexist(monkeypatch):
    monkeypatch.setattr(xlovecam_mod.requests, 'post', _fail_post)
    bot = XLoveCam('alice')
    assert bot.getStatus() == Status.UNKNOWN


def test_xlovecam_not_in_online_list_is_offline(monkeypatch):
    # The onlineList endpoint only returns online performers: absence can mean
    # "offline" just as well as "nonexistent", so it must never stop the
    # monitor via NOTEXIST.
    class _Resp:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {'content': {'performerList': []}}

    monkeypatch.setattr(xlovecam_mod.requests, 'post', lambda *a, **kw: _Resp())
    bot = XLoveCam('alice')
    assert bot.getStatus() == Status.OFFLINE


def test_xlovecam_resolves_id_on_a_later_poll(monkeypatch):
    monkeypatch.setattr(xlovecam_mod.requests, 'post', _fail_post)
    bot = XLoveCam('alice')
    assert bot._id is None

    class _Resp:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {'content': {'performerList': [
                {'nickname': 'Alice', 'id': 4242}]}}

    monkeypatch.setattr(xlovecam_mod.requests, 'post', lambda *a, **kw: _Resp())
    assert bot._resolve_performer_id() == 4242


# --- RoomIdBot (SexChatHU): startup failure must not become permanent NOTEXIST ---

def test_sexchathu_constructor_survives_list_failure(monkeypatch):
    monkeypatch.setattr(SexChatHU, '_getBabesList',
                        classmethod(lambda cls, force_update=False: (_ for _ in ()).throw(
                            requests.exceptions.ConnectionError('down'))))
    bot = SexChatHU('somebody')
    assert bot.room_id is None
    # UNKNOWN keeps the monitor polling; NOTEXIST would set running=False and
    # could be persisted, silently ending monitoring forever.
    assert bot.sc == Status.UNKNOWN


def test_sexchathu_status_retries_room_id(monkeypatch):
    monkeypatch.setattr(SexChatHU, '_getBabesList',
                        classmethod(lambda cls, force_update=False: (_ for _ in ()).throw(
                            requests.exceptions.ConnectionError('down'))))
    bot = SexChatHU('somebody')
    assert bot.getStatus() == Status.UNKNOWN

    monkeypatch.setattr(SexChatHU, '_getBabesList',
                        classmethod(lambda cls, force_update=False: [
                            {'perfid': '123', 'screenname': 'somebody'}]))
    assert bot._ensure_room_id() is True
    assert bot.room_id == '123'


# --- StripChat: mouflon key chain must not poison-cache failures ---

@pytest.fixture
def stripchat_state(monkeypatch, tmp_path):
    # Fresh key dict (no keys leaked from the real on-disk cache) and a
    # throwaway cache file so _save_mouflon_cache can never write into the
    # repository's stripchat_mouflon_keys.json.
    monkeypatch.setattr(StripChat, '_mouflon_keys', {'known': 'known-value'})
    monkeypatch.setattr(StripChat, '_mouflon_cache_filename',
                        str(tmp_path / 'mouflon.json'))
    monkeypatch.setattr(StripChat, '_doppio_js_data', None)
    monkeypatch.setattr(StripChat, '_static_data', StripChat._static_data)
    monkeypatch.setattr(StripChat, '_init_failed_at', None)
    monkeypatch.setattr(StripChat, '_ensure_initial_data',
                        classmethod(lambda cls: None))
    yield


def test_mouflon_key_miss_is_not_cached_forever(stripchat_state, monkeypatch):
    # lru_cache used to memoize the None: once a key lookup missed, the same
    # pkey returned None for the life of the process even after the key became
    # available, killing StripChat recording until a restart.
    assert StripChat.getMouflonDecKey('freshpkey123') is None
    monkeypatch.setattr(StripChat, '_doppio_js_data',
                        'junk "freshpkey123:the-secret-key" junk')
    assert StripChat.getMouflonDecKey('freshpkey123') == 'the-secret-key'


def test_mouflon_extracted_key_is_persisted(stripchat_state, monkeypatch, tmp_path):
    cache_file = tmp_path / 'mouflon.json'
    monkeypatch.setattr(StripChat, '_mouflon_cache_filename', str(cache_file))
    monkeypatch.setattr(StripChat, '_doppio_js_data',
                        '"pkeyabc:extracted-key-1"')
    assert StripChat.getMouflonDecKey('pkeyabc') == 'extracted-key-1'
    saved = json.loads(cache_file.read_text())
    assert saved['pkeyabc'] == 'extracted-key-1'


def test_initial_data_failure_backs_off_then_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(StripChat, '_static_data', None)
    monkeypatch.setattr(StripChat, '_init_failed_at', None)
    monkeypatch.setattr(StripChat, 'getInitialData',
                        classmethod(lambda cls: calls.append(1)))
    StripChat._ensure_initial_data()
    assert calls == [1]
    assert StripChat._static_data is StripChat._INIT_FAILED

    # Within the backoff window: no second attempt.
    StripChat._ensure_initial_data()
    assert calls == [1]

    # Window elapsed: a retry is allowed (one startup hiccup is not forever).
    monkeypatch.setattr(StripChat, '_INIT_RETRY_INTERVAL', 0)
    StripChat._ensure_initial_data()
    assert calls == [1, 1]
