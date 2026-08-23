"""Every converted adapter must answer UNKNOWN (not raise, not misclassify)
when the site's API fails at the transport, HTTP or JSON layer."""
from unittest import mock

import pytest
import requests

from streamonitor.enums import Status
from streamonitor.sites.bongacams import BongaCams
from streamonitor.sites.camscom import CamsCom
from streamonitor.sites.chaturbate import Chaturbate
from streamonitor.sites.cherrytv import CherryTV
from streamonitor.sites.streamate import StreaMate


class _Resp:
    def __init__(self, status_code=200, body=None, raw=b'not json'):
        self.status_code = status_code
        self._body = body
        self.content = raw
        self.text = raw.decode() if isinstance(raw, bytes) else raw

    def json(self):
        if self._body is None:
            raise requests.exceptions.JSONDecodeError('bad', 'doc', 0)
        return self._body


def _bot(cls, username='alice'):
    bot = cls(username)
    return bot


def _stub_session(bot, response):
    def fake_request(method, url, **kwargs):
        if isinstance(response, Exception):
            raise response
        return response
    bot.session = mock.Mock()
    bot.session.request = fake_request
    bot.session.get = lambda url, **kw: fake_request('GET', url, **kw)
    bot.session.post = lambda url, **kw: fake_request('POST', url, **kw)


@pytest.mark.parametrize('cls', [CherryTV, CamsCom, BongaCams, Chaturbate])
@pytest.mark.parametrize('failure', [
    requests.exceptions.ConnectionError('down'),
    _Resp(status_code=502, raw=b'<html>Bad Gateway</html>'),
    _Resp(status_code=200, body=None),          # unparseable JSON
])
def test_api_failures_map_to_unknown(cls, failure):
    bot = _bot(cls)
    _stub_session(bot, failure)
    assert bot.getStatus() == Status.UNKNOWN


@pytest.mark.parametrize('cls', [CherryTV, BongaCams, Chaturbate])
def test_unexpected_response_shape_maps_to_unknown(cls):
    # CamsCom is deliberately excluded: its API signals "no such model" by
    # omitting stream_name, so a missing key IS its NOTEXIST contract.
    bot = _bot(cls)
    _stub_session(bot, _Resp(status_code=200, body={'totally': 'unexpected'}))
    assert bot.getStatus() == Status.UNKNOWN


def test_chaturbate_real_429_still_maps_to_ratelimit():
    bot = _bot(Chaturbate)
    _stub_session(bot, _Resp(status_code=429, raw=b'slow down'))
    assert bot.getStatus() == Status.RATELIMIT
    assert bot.ratelimit is True


def test_chaturbate_connection_error_is_not_ratelimit():
    # The old bare except turned DNS failures and JSON changes into RATELIMIT,
    # hiding real breakage behind a 180s backoff.
    bot = _bot(Chaturbate)
    _stub_session(bot, requests.exceptions.ConnectionError('down'))
    assert bot.getStatus() == Status.UNKNOWN
    assert bot.ratelimit is False


def test_streamate_known_codes_keep_their_mapping():
    bot = _bot(StreaMate)
    for code, expected in [(403, Status.PRIVATE), (404, Status.OFFLINE),
                           (429, Status.RATELIMIT)]:
        _stub_session(bot, _Resp(status_code=code, raw=b''))
        assert bot.getStatus() == expected


def test_streamate_unmapped_code_is_unknown_not_valueerror():
    bot = _bot(StreaMate)
    _stub_session(bot, _Resp(status_code=502, raw=b''))
    assert bot.getStatus() == Status.UNKNOWN


def test_streamate_200_is_public_with_lastinfo():
    bot = _bot(StreaMate)
    _stub_session(bot, _Resp(status_code=200, body={'formats': {}}))
    assert bot.getStatus() == Status.PUBLIC
    assert bot.lastInfo == {'formats': {}}
