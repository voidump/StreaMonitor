import json
import os
import threading
import time
import types
from contextlib import closing

import pytest

import parameters
import streamonitor.media_index as media_index
import streamonitor.nsfw_backfill as backfill


OLD_MTIME = 1_700_000_000
AUTH = {"Authorization": "Basic YWRtaW46YWRtaW4="}   # admin:admin


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    # The back-scan session is module state; reset it so tests never leak into
    # each other. MIN_VIDEO_SIZE_MB would delete the tiny fixture files.
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 0)
    monkeypatch.setattr(parameters, "NSFW_ENABLED", True)
    backfill._cancel.clear()
    backfill._state.update({'running': False, 'total': 0, 'done': 0, 'failed': 0,
                            'current_file': None, 'started_ts': None})
    yield
    backfill._cancel.set()
    if backfill._thread is not None:
        backfill._thread.join(timeout=30)
    backfill._thread = None
    backfill._cancel.clear()


def _make_video(tmp_path, filename, username="alice", site="FS", mtime=OLD_MTIME,
                subdir="downloads"):
    folder = tmp_path / subdir / f"{username} [{site}]"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(b"0" * 1000)
    os.utime(path, (mtime, mtime))
    return str(path)


def _classification(abs_path):
    with closing(media_index.connect()) as conn:
        row = conn.execute("SELECT classification FROM media_files WHERE abs_path=?",
                           (os.path.abspath(abs_path),)).fetchone()
    return row and row[0]


def _client(streamers=None):
    from streamonitor.managers.httpmanager import HTTPManager
    return HTTPManager(streamers or []).create_app().test_client()


def _run_and_wait(streamers=None):
    state, started = backfill.start(streamers)
    backfill._thread.join(timeout=30)
    assert not backfill._thread.is_alive()
    return state, started


def test_backfill_labels_all_pending_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = _make_video(tmp_path, "alice-1.mp4")
    b = _make_video(tmp_path, "alice-2.mp4")
    media_index.scan_once()
    monkeypatch.setattr(backfill, "analyze_file", lambda p: ("adult", [0.9]))

    _run_and_wait()

    assert _classification(a) == "adult"
    assert _classification(b) == "adult"
    progress = backfill.progress()
    assert progress["running"] is False
    assert (progress["total"], progress["done"], progress["failed"]) == (2, 2, 0)
    assert progress["pending"] == 0


def test_backfill_skips_already_labeled_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    labeled = _make_video(tmp_path, "alice-1.mp4")
    media_index.scan_once()
    media_index.set_classification(labeled, "non-adult")
    monkeypatch.setattr(backfill, "analyze_file",
                        lambda p: pytest.fail("labeled row re-analyzed"))

    _run_and_wait()

    assert _classification(labeled) == "non-adult"
    assert backfill.progress()["total"] == 0


def test_backfill_counts_analysis_failures(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path, "alice-1.mp4")
    media_index.scan_once()

    def boom(_path):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(backfill, "analyze_file", boom)

    _run_and_wait()

    progress = backfill.progress()
    assert (progress["done"], progress["failed"]) == (0, 1)
    assert progress["running"] is False
    assert _classification(path) is None


def test_backfill_start_is_idempotent_while_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path, "alice-1.mp4")
    media_index.scan_once()
    release = threading.Event()

    def slow_analyze(_path):
        release.wait(30)
        return "adult", [0.9]

    monkeypatch.setattr(backfill, "analyze_file", slow_analyze)
    first, started_first = backfill.start()
    second, started_second = backfill.start()
    release.set()
    backfill._thread.join(timeout=30)

    assert started_first is True and started_second is False
    assert second["running"] is True
    assert second["started_ts"] == first["started_ts"]


def test_backfill_cancel_stops_after_current_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("alice-1.mp4", "alice-2.mp4", "alice-3.mp4"):
        _make_video(tmp_path, name)
    media_index.scan_once()
    monkeypatch.setattr(backfill, "analyze_file",
                        lambda p: (backfill.cancel(), ("adult", [0.9]))[1])

    _run_and_wait()

    progress = backfill.progress()
    assert progress["running"] is False
    assert progress["done"] == 1          # the in-flight file was still labeled
    assert progress["pending"] == 2


def test_backfill_stops_when_nsfw_disabled_mid_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path, "alice-1.mp4")
    _make_video(tmp_path, "alice-2.mp4")
    media_index.scan_once()

    def analyze_then_disable(_path):
        monkeypatch.setattr(parameters, "NSFW_ENABLED", False)
        return "adult", [0.9]

    monkeypatch.setattr(backfill, "analyze_file", analyze_then_disable)

    _run_and_wait()

    assert backfill.progress()["done"] == 1


# --- exclusion rules (same standard as apply_nsfw_action) ---

def test_pending_paths_applies_exclusion_rules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    eligible = _make_video(tmp_path, "bob-1.mp4", username="bob")
    fresh = _make_video(tmp_path, "bob-fresh.mp4", username="bob",
                        mtime=int(time.time()))
    recording = _make_video(tmp_path, "alice-recording.mp4")
    sibling = _make_video(tmp_path, "alice-segment_001.mp4")
    recycled = _make_video(tmp_path, "carol-1.mp4", username="carol",
                           subdir="downloads/_recycle")
    archived = _make_video(tmp_path, "dave-1.mp4", username="dave",
                           subdir="downloads/_nonadult")
    media_index.scan_once()
    streamer = types.SimpleNamespace(
        recording=True,
        recording_filename="alice-recording.mp4",
        outputFolder=os.path.dirname(recording),
        username="alice",
    )

    pending = backfill.pending_paths([streamer])

    assert pending == [os.path.abspath(eligible)]
    for excluded in (fresh, recording, sibling, recycled, archived):
        assert os.path.abspath(excluded) not in pending


def test_pending_paths_excludes_tmp_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # scan_once never indexes '.tmp.' names, so insert the row directly.
    tmp_video = _make_video(tmp_path, "alice-1.tmp.mp4")
    media_index.set_classification(tmp_video, "adult")
    with closing(media_index.connect()) as conn:
        conn.execute("UPDATE media_files SET classification=NULL")
        conn.commit()

    assert backfill.pending_paths() == []


def test_pending_paths_empty_without_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert backfill.pending_paths() == []
    assert not os.path.exists(media_index.index_db_path())


# --- API ---

def test_backscan_endpoints_require_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = _client()
    assert client.get("/api/v2/settings/nsfw/backscan/progress").status_code == 401
    assert client.post("/api/v2/settings/nsfw/backscan/start").status_code == 401
    assert client.post("/api/v2/settings/nsfw/backscan/cancel").status_code == 401


def test_backscan_start_refused_while_nsfw_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(parameters, "NSFW_ENABLED", False)

    response = _client().post("/api/v2/settings/nsfw/backscan/start", headers=AUTH)

    assert response.status_code == 409
    assert json.loads(response.data)["code"] == "disabled"
    assert backfill.progress()["running"] is False


def test_backscan_start_progress_cancel_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path, "alice-1.mp4")
    _make_video(tmp_path, "alice-2.mp4")
    media_index.scan_once()
    entered = threading.Event()
    release = threading.Event()

    def slow_analyze(_path):
        entered.set()
        release.wait(30)
        return "adult", [0.9]

    monkeypatch.setattr(backfill, "analyze_file", slow_analyze)
    client = _client()

    started = json.loads(client.post(
        "/api/v2/settings/nsfw/backscan/start", headers=AUTH).data)
    assert started["running"] is True and started["started_ts"]
    assert entered.wait(30)   # the first file is being analyzed

    progress = json.loads(client.get(
        "/api/v2/settings/nsfw/backscan/progress", headers=AUTH).data)
    assert progress["running"] is True
    assert set(progress) >= {"running", "total", "done", "failed",
                             "current_file", "started_ts"}

    # A second start never launches a parallel session.
    again = json.loads(client.post(
        "/api/v2/settings/nsfw/backscan/start", headers=AUTH).data)
    assert again["started_ts"] == started["started_ts"]

    cancelled = json.loads(client.post(
        "/api/v2/settings/nsfw/backscan/cancel", headers=AUTH).data)
    assert cancelled["running"] is True   # stops after the current file finishes
    release.set()
    backfill._thread.join(timeout=30)

    final = json.loads(client.get(
        "/api/v2/settings/nsfw/backscan/progress", headers=AUTH).data)
    assert final["running"] is False and final["current_file"] is None
    assert final["done"] == 1
