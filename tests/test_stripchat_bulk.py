import logging
import re

import requests

from streamonitor.enums import Status
from streamonitor.sites.stripchat import StripChat


def make_sc(room_id, sc=Status.OFFLINE):
    """Build a StripChat instance without running its network __init__."""
    s = StripChat.__new__(StripChat)
    s.room_id = room_id
    s.sc = sc
    s.previous_status = None
    s.gender = None
    s.country = None
    s.lastCheckTs = None
    s.username = f"user{room_id}"
    s.logger = logging.getLogger("test_stripchat_bulk")
    s._bulk_absent_count = 0
    return s


class FakeResp:
    def __init__(self, payload, raise_json=False):
        self._payload = payload
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise requests.exceptions.JSONDecodeError("Expecting value", "", 0)
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.headers = {}
        self._handler = handler

    def get(self, url, timeout=None):
        return self._handler(url)


def patch_session(monkeypatch, handler):
    monkeypatch.setattr(
        "streamonitor.sites.stripchat.requests.Session",
        lambda: FakeSession(handler),
    )


def ids_from_url(url):
    return re.findall(r"modelIds\[\]=(\d+)", url)


def test_room_id_int_matches_str_response(monkeypatch):
    # room_id is an int; data_map keys are str(model['id']). Matching must still
    # work because getStatusBulk normalizes keys with str().
    streamer = make_sc(12345, sc=Status.NOTRUNNING)

    def handler(url):
        return FakeResp({"models": [{"id": 12345, "status": "public"}]})

    patch_session(monkeypatch, handler)
    StripChat.getStatusBulk([streamer])

    assert streamer.sc == Status.PUBLIC
    assert streamer.lastCheckTs is not None


def test_missing_model_keeps_previous_status(monkeypatch):
    # A model absent from the bulk response must not be forced to UNKNOWN
    # (which strands it in the frontend "checking" dead-end).
    streamer = make_sc("999", sc=Status.OFFLINE)

    def handler(url):
        return FakeResp({"models": []})

    patch_session(monkeypatch, handler)
    StripChat.getStatusBulk([streamer])

    assert streamer.sc == Status.OFFLINE
    assert streamer.lastCheckTs is None


def test_json_failure_skips_only_that_batch(monkeypatch):
    # 150 streamers -> two batches (100 + 50). The second batch fails to parse
    # JSON; the first batch's streamers must still be updated.
    streamers = [make_sc(str(i), sc=Status.OFFLINE) for i in range(150)]
    calls = {"n": 0}

    def handler(url):
        calls["n"] += 1
        if calls["n"] == 2:
            return FakeResp(None, raise_json=True)
        ids = ids_from_url(url)
        return FakeResp({"models": [{"id": int(i), "status": "public"} for i in ids]})

    patch_session(monkeypatch, handler)
    StripChat.getStatusBulk(streamers)

    # First batch (indices 0-99) parsed successfully.
    assert streamers[0].sc == Status.PUBLIC
    assert streamers[99].sc == Status.PUBLIC
    assert streamers[0].lastCheckTs is not None
    # Second batch (indices 100-149) failed: previous status preserved.
    assert streamers[100].sc == Status.OFFLINE
    assert streamers[149].sc == Status.OFFLINE
    assert streamers[149].lastCheckTs is None


def test_unknown_status_is_set_and_timestamped(monkeypatch):
    # An explicit unrecognized status still marks UNKNOWN and stamps the check,
    # so bot.py's single getStatus fallback can recover it.
    streamer = make_sc("42", sc=Status.OFFLINE)

    def handler(url):
        return FakeResp({"models": [{"id": 42, "status": "somethingWeird"}]})

    patch_session(monkeypatch, handler)
    StripChat.getStatusBulk([streamer])

    assert streamer.sc == Status.UNKNOWN
    assert streamer.lastCheckTs is not None


def test_persistent_absence_marks_notexist(monkeypatch):
    # A deleted/banned account stays absent from the bulk response forever.
    # Below the threshold the previous status is kept; once reached the
    # streamer is marked NOTEXIST so it leaves the "checking" dead-end.
    streamer = make_sc("777", sc=Status.OFFLINE)

    def handler(url):
        return FakeResp({"models": []})

    patch_session(monkeypatch, handler)

    for _ in range(StripChat._BULK_ABSENT_THRESHOLD - 1):
        StripChat.getStatusBulk([streamer])
        assert streamer.sc == Status.OFFLINE
        assert streamer.lastCheckTs is None

    StripChat.getStatusBulk([streamer])
    assert streamer.sc == Status.NOTEXIST
    assert streamer.lastCheckTs is not None


def test_reappearance_resets_absence_and_status(monkeypatch):
    # Once the account shows up again the counter is cleared and the reported
    # status applies, so the streamer automatically leaves NOTEXIST.
    streamer = make_sc("778", sc=Status.OFFLINE)
    present = {"v": False}

    def handler(url):
        if present["v"]:
            return FakeResp({"models": [{"id": 778, "status": "public"}]})
        return FakeResp({"models": []})

    patch_session(monkeypatch, handler)

    for _ in range(StripChat._BULK_ABSENT_THRESHOLD):
        StripChat.getStatusBulk([streamer])
    assert streamer.sc == Status.NOTEXIST

    present["v"] = True
    StripChat.getStatusBulk([streamer])
    assert streamer.sc == Status.PUBLIC
    assert streamer._bulk_absent_count == 0

    # A later single absent round must not immediately fall back to NOTEXIST.
    present["v"] = False
    StripChat.getStatusBulk([streamer])
    assert streamer.sc == Status.PUBLIC


def test_absent_count_defaults_without_instance_attribute(monkeypatch):
    # Instances built by __init__ rely on the class-level default.
    streamer = make_sc("779", sc=Status.OFFLINE)
    del streamer._bulk_absent_count

    def handler(url):
        return FakeResp({"models": []})

    patch_session(monkeypatch, handler)
    StripChat.getStatusBulk([streamer])

    assert streamer._bulk_absent_count == 1
    assert streamer.sc == Status.OFFLINE
