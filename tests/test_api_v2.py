import base64
import json
import os
import shutil
import subprocess

import pytest

import parameters
import streamonitor.config as config
from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.managers.httpmanager import HTTPManager


def basic_auth(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


AUTH = basic_auth("admin", "admin")


class FakeSite(Bot):
    site = "FakeSite"
    siteslug = "FS"
    aliases = []

    def run(self):
        pass

    def start(self):
        self._alive = True

    def is_alive(self):
        return getattr(self, "_alive", False)

    def stop(self, a, b, thread_too=False):
        self.running = False

    def restart(self):
        self.running = True

    def getWebsiteURL(self):
        return f"https://fake/{self.username}"


# Keep the global site registry clean for the read-only tests above; individual
# tests that exercise do_add opt in via the fake_site fixture.
LOADED_SITES.discard(FakeSite)


@pytest.fixture
def fake_site():
    LOADED_SITES.add(FakeSite)
    yield FakeSite
    LOADED_SITES.discard(FakeSite)


def make_streamer(username="alice", running=True, monitor_state="active"):
    streamer = FakeSite(username)
    streamer.running = running
    streamer.monitorState = monitor_state
    return streamer


@pytest.fixture
def client():
    manager = HTTPManager([])
    app = manager.create_app()
    return app.test_client()


def test_api_v2_streamers_requires_auth(client):
    response = client.get("/api/v2/streamers")
    assert response.status_code == 401


def test_api_v2_streamers_returns_json_list(client):
    response = client.get("/api/v2/streamers", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert json.loads(response.data) == []


def test_api_v2_streamer_detail_unknown_returns_404(client):
    response = client.get("/api/v2/streamers/SC/nobody", headers=AUTH)
    assert response.status_code == 404
    assert json.loads(response.data)["error"] == "Streamer not found"


def test_api_v2_basesettings_shape(client):
    response = client.get("/api/v2/basesettings", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert "sites" in payload
    assert "status" in payload
    disk = payload["diskSpace"]
    for key in ("total", "free", "used", "percentageFree", "freeHuman", "totalHuman"):
        assert key in disk


@pytest.fixture
def webui_client(monkeypatch):
    monkeypatch.setattr(
        "streamonitor.managers.httpmanager.httpmanager.WEBSERVER_SKIN", "webui"
    )
    manager = HTTPManager([])
    app = manager.create_app()
    return app.test_client()


def test_webui_root_serves_console(webui_client):
    response = webui_client.get("/", headers=AUTH)
    assert response.status_code == 200
    assert b"StreaMonitor" in response.data


def test_webui_serves_js_assets(webui_client):
    response = webui_client.get("/js/main.js", headers=AUTH)
    assert response.status_code == 200


def test_webui_serves_css_assets(webui_client):
    response = webui_client.get("/css/console.css", headers=AUTH)
    assert response.status_code == 200


def test_basesettings_includes_web_list_frequency(client):
    response = client.get("/api/v2/basesettings", headers=AUTH)
    payload = json.loads(response.data)
    assert "webListFrequency" in payload
    assert isinstance(payload["webListFrequency"], int)


def test_basesettings_auth_enabled_when_password_set(client):
    response = client.get("/api/v2/basesettings", headers=AUTH)
    payload = json.loads(response.data)
    assert payload["authEnabled"] is True


def test_basesettings_auth_disabled_when_password_empty(monkeypatch):
    # An empty password disables auth entirely, so the frontend must hide the
    # logout entry.
    monkeypatch.setattr(parameters, "WEBSERVER_PASSWORD", "")
    client = HTTPManager([]).create_app().test_client()
    response = client.get("/api/v2/basesettings")
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["authEnabled"] is False


# --- monitorState persistence round-trip ---

def test_monitor_state_export_fromconfig_roundtrip():
    streamer = make_streamer(monitor_state="stopped")
    exported = streamer.export()
    assert exported["monitorState"] == "stopped"

    restored = FakeSite.fromConfig(exported)
    assert restored.monitorState == "stopped"


# --- manager three-layer semantics (unit) ---

def test_manager_pause_resume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer(running=True)
    manager = HTTPManager([streamer])

    assert manager.do_pause(streamer, "alice", "FakeSite") == "OK"
    assert streamer.running is False
    assert manager.do_resume(streamer, "alice", "FakeSite") == "OK"
    assert streamer.running is True


def test_manager_stop_recording_keeps_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer(running=True)
    manager = HTTPManager([streamer])

    assert manager.do_stop_recording(streamer, "alice", "FakeSite") == "OK"
    assert streamer.monitorState == "stopped"
    assert streamer.running is False
    assert streamer in manager.streamers
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved[0]["monitorState"] == "stopped"


def test_manager_reactivate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer(running=False, monitor_state="stopped")
    manager = HTTPManager([streamer])

    assert manager.do_reactivate(streamer, "alice", "FakeSite") == "OK"
    assert streamer.monitorState == "active"
    assert streamer.running is True


def test_manager_purge_writes_history_and_removes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr("streamonitor.manager.shutil.rmtree", lambda p, **k: calls.append(p))
    streamer = make_streamer()
    manager = HTTPManager([streamer])

    assert manager.do_purge(streamer, "alice", "FakeSite", delete_files=False) == "OK"
    assert streamer not in manager.streamers
    assert calls == []
    history = config.load_history()
    assert history[0]["username"] == "alice"
    assert history[0]["site"] == "FakeSite"
    assert "deletedAt" in history[0]


def test_manager_purge_deletes_files_when_requested(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr("streamonitor.manager.shutil.rmtree", lambda p, **k: calls.append(p))
    streamer = make_streamer()
    manager = HTTPManager([streamer])

    assert manager.do_purge(streamer, "alice", "FakeSite", delete_files=True) == "OK"
    assert calls == [streamer.outputFolder]


# --- API write endpoints ---

def _client(manager):
    return manager.create_app().test_client()


def test_api_action_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).post("/api/v2/streamers/FakeSite/alice/pause")
    assert response.status_code == 401


def test_api_pause_returns_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer(running=True)
    manager = HTTPManager([streamer])
    response = _client(manager).post("/api/v2/streamers/FakeSite/alice/pause", headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["running"] is False


def test_api_stop_recording_persists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer(running=True)
    manager = HTTPManager([streamer])
    response = _client(manager).post(
        "/api/v2/streamers/FakeSite/alice/stop-recording", headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["monitorState"] == "stopped"


def test_api_unknown_action_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).post("/api/v2/streamers/FakeSite/alice/bogus", headers=AUTH)
    assert response.status_code == 404


def test_api_action_missing_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).post("/api/v2/streamers/FakeSite/ghost/pause", headers=AUTH)
    assert response.status_code == 404


def test_api_delete_purges_and_writes_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("streamonitor.manager.shutil.rmtree", lambda p, **k: None)
    streamer = make_streamer()
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice", json={"deleteFiles": False}, headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["ok"] is True
    assert streamer not in manager.streamers
    assert config.load_history()[0]["username"] == "alice"


def test_api_batch_per_item_results(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer(running=True)
    manager = HTTPManager([streamer])
    response = _client(manager).post(
        "/api/v2/batch",
        json={"action": "pause", "targets": [
            {"site": "FakeSite", "user": "alice"},
            {"site": "FakeSite", "user": "ghost"},
        ]},
        headers=AUTH,
    )
    assert response.status_code == 200
    results = json.loads(response.data)["results"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert streamer.running is False


def test_api_batch_unknown_action_400(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).post(
        "/api/v2/batch", json={"action": "bogus", "targets": []}, headers=AUTH)
    assert response.status_code == 400


def test_api_add_streamer(tmp_path, monkeypatch, fake_site):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).post(
        "/api/v2/streamers", json={"username": "bob", "site": "FakeSite"}, headers=AUTH)
    assert response.status_code == 201
    assert json.loads(response.data)["username"] == "bob"
    assert manager.getStreamer("bob", "FakeSite") is not None


def test_api_add_streamer_missing_fields_400(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).post("/api/v2/streamers", json={"username": "bob"}, headers=AUTH)
    assert response.status_code == 400


def test_api_history_get(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([{"username": "alice", "site": "FakeSite", "deletedAt": 1}])
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/history", headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)[0]["username"] == "alice"


def test_api_history_readd(tmp_path, monkeypatch, fake_site):
    monkeypatch.chdir(tmp_path)
    config.save_history([
        {"username": "alice", "site": "FakeSite", "url": "https://fake/alice", "deletedAt": 1}])
    manager = HTTPManager([])
    response = _client(manager).post("/api/v2/history/0/readd", headers=AUTH)
    assert response.status_code == 201
    assert manager.getStreamer("alice", "FakeSite") is not None
    assert config.load_history() == []


def test_api_history_delete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([{"username": "alice", "site": "FakeSite", "deletedAt": 1}])
    manager = HTTPManager([])
    response = _client(manager).delete("/api/v2/history/0", headers=AUTH)
    assert response.status_code == 200
    assert config.load_history() == []


def test_api_history_readd_out_of_range_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([])
    manager = HTTPManager([])
    response = _client(manager).post("/api/v2/history/5/readd", headers=AUTH)
    assert response.status_code == 404


def test_api_history_avatar_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([{"username": "alice", "site": "FakeSite", "deletedAt": 1}])
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/history/0/avatar")
    assert response.status_code == 401


def test_api_history_avatar_out_of_range_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([])
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/history/5/avatar", headers=AUTH)
    assert response.status_code == 404


def test_api_history_avatar_no_cache_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([{"username": "alice", "site": "FakeSite", "deletedAt": 1}])
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/history/0/avatar", headers=AUTH)
    assert response.status_code == 404


def test_api_history_avatar_serves_cached_image(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.save_history([{"username": "alice", "site": "FakeSite", "deletedAt": 1}])
    cache_path = api_v2._avatar_cache_path("FakeSite", "alice")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(_PNG_BYTES)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/history/0/avatar", headers=AUTH)
    assert response.status_code == 200
    assert response.data == _PNG_BYTES
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, max-age=3600"


def test_purge_delete_files_preserves_avatar_cache(tmp_path, monkeypatch):
    # V33C: permanently deleting a streamer (even with delete_files) must not
    # remove its cached avatar, so the history page can still show the real face.
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    cache_path = api_v2._avatar_cache_path(streamer.site, streamer.username)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(_PNG_BYTES)
    manager = HTTPManager([streamer])

    assert manager.do_purge(streamer, "alice", "FakeSite", delete_files=True) == "OK"
    assert os.path.exists(cache_path)


# --- video streaming (Range) + deletion ---

def _make_streamer_with_video(filename="alice-20260706-120000.mp4", data=b"0" * 1000):
    streamer = make_streamer()
    os.makedirs(streamer.outputFolder, exist_ok=True)
    path = os.path.join(streamer.outputFolder, filename)
    with open(path, "wb") as f:
        f.write(data)
    streamer.cache_file_list()
    return streamer, path


def test_video_supports_range_requests(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video(data=b"0123456789" * 100)
    manager = HTTPManager([streamer])
    client = _client(manager)

    partial = client.get(
        "/video/alice/FS/alice-20260706-120000.mp4",
        headers={**AUTH, "Range": "bytes=0-99"},
    )
    assert partial.status_code == 206
    assert partial.headers["Content-Range"] == "bytes 0-99/1000"
    assert len(partial.data) == 100

    full = client.get("/video/alice/FS/alice-20260706-120000.mp4", headers=AUTH)
    assert full.status_code == 200
    assert full.headers["Accept-Ranges"] == "bytes"


def _make_recycled_video(streamer, filename="alice-20260706-120000.mp4",
                         data=b"0" * 1000):
    # A file moved by the NSFW recycle action: same "<username> [<siteslug>]"
    # folder layout under the recycle-bin dir instead of the output folder.
    os.makedirs(streamer.recycleFolder, exist_ok=True)
    path = os.path.join(streamer.recycleFolder, filename)
    with open(path, "wb") as f:
        f.write(data)
    streamer.cache_file_list()
    return path


def test_video_falls_back_to_recycle_folder(tmp_path, monkeypatch):
    # Regression: a recycled video's /video URL kept 404ing because get_video
    # only looked in the output and non-adult folders.
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    _make_recycled_video(streamer)
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/video/alice/FS/alice-20260706-120000.mp4", headers=AUTH)
    assert response.status_code == 200
    assert response.data == b"0" * 1000


def test_cache_file_list_includes_recycle_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    path = _make_recycled_video(streamer)
    assert [v.abs_path for v in streamer.video_files] == [os.path.abspath(path)]


def test_video_thumb_recycled_file_is_found(tmp_path, monkeypatch):
    # The thumb endpoint's disk-scan fallback must see recycled files (the
    # cached file list includes the recycle folder), so the response is not
    # a "Video not found" 404. Thumb generation itself is stubbed out.
    import streamonitor.managers.httpmanager.api_v2 as api_v2_mod
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    _make_recycled_video(streamer)

    def fake_generate(abs_path, thumb_path, mtime, duration=None, allow_probe=True):
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with open(thumb_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
        return True

    monkeypatch.setattr(api_v2_mod, "_generate_thumb_limited", fake_generate)
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-120000.mp4/thumb",
        headers=AUTH)
    assert response.status_code == 200


def test_api_delete_video_removes_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, path = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-120000.mp4", headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["ok"] is True
    assert not os.path.exists(path)


def test_api_delete_video_missing_returns_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice/videos/nope.mp4", headers=AUTH)
    assert response.status_code == 404


def test_api_delete_video_unknown_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/ghost/videos/x.mp4", headers=AUTH)
    assert response.status_code == 404


def test_api_delete_video_protects_recording(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, path = _make_streamer_with_video()
    streamer.recording = True
    streamer.recording_filename = "alice-20260706-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-120000.mp4", headers=AUTH)
    assert response.status_code == 409
    assert os.path.exists(path)


def _add_video(streamer, filename, data=b"0" * 1000):
    path = os.path.join(streamer.outputFolder, filename)
    with open(path, "wb") as f:
        f.write(data)
    streamer.cache_file_list()
    return path


def test_api_delete_all_videos_removes_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, p1 = _make_streamer_with_video("alice-20260706-120000.mp4")
    p2 = _add_video(streamer, "alice-20260707-120000.mp4")
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice/videos", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["ok"] is True
    assert payload["deleted"] == 2
    assert payload["skipped"] == 0
    assert not os.path.exists(p1)
    assert not os.path.exists(p2)


def test_api_delete_all_videos_skips_recording(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, old = _make_streamer_with_video("alice-20260706-120000.mp4")
    rec = _add_video(streamer, "alice-20260707-120000.mp4")
    streamer.recording = True
    streamer.recording_filename = "alice-20260707-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice/videos", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["deleted"] == 1
    assert payload["skipped"] == 1
    assert not os.path.exists(old)
    assert os.path.exists(rec)


def test_api_delete_all_videos_empty_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    manager = HTTPManager([streamer])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/alice/videos", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["ok"] is True
    assert payload["deleted"] == 0
    assert payload["skipped"] == 0


def test_api_delete_all_videos_unknown_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).delete(
        "/api/v2/streamers/FakeSite/ghost/videos", headers=AUTH)
    assert response.status_code == 404


def test_api_detail_flags_recording_video(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    streamer.recording = True
    streamer.recording_filename = "alice-20260706-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["recordingFilename"] == "alice-20260706-120000.mp4"
    assert payload["videos"][0]["recording"] is True


# --- last recorded timestamp on the list card (NB15) ---

def test_streamers_list_last_recorded_ts_is_max_mtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, path = _make_streamer_with_video()
    os.utime(path, (1_700_000_000, 1_700_000_000))
    newer = os.path.join(streamer.outputFolder, "alice-20260707-120000.mp4")
    with open(newer, "wb") as f:
        f.write(b"0" * 10)
    os.utime(newer, (1_700_000_500, 1_700_000_500))
    streamer.cache_file_list()
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers", headers=AUTH)
    assert response.status_code == 200
    item = json.loads(response.data)[0]
    assert item["lastRecordedTs"] == 1_700_000_500


def test_streamers_list_last_recorded_ts_null_when_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers", headers=AUTH)
    assert response.status_code == 200
    item = json.loads(response.data)[0]
    assert item["lastRecordedTs"] is None


# --- live view (watch-while-recording) endpoint (L1) ---

import streamonitor.managers.httpmanager.api_v2 as api_v2


def _start_recording(streamer, recording_filename="alice-20260707-120000.mp4",
                     ts_data=b"\x47" + b"\x00" * 187):
    # Simulate an in-progress native-HLS recording by creating the .tmp.ts file
    # that getVideoNativeHLS would be appending to.
    streamer.recording = True
    streamer.recording_filename = recording_filename
    os.makedirs(streamer.outputFolder, exist_ok=True)
    base, _ext = os.path.splitext(recording_filename)
    tmp_path = os.path.join(streamer.outputFolder, base + ".tmp.ts")
    with open(tmp_path, "wb") as f:
        f.write(ts_data)
    return tmp_path


def test_live_available_true_when_recording_ts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    _start_recording(streamer)
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(response.data)["liveAvailable"] is True


def test_live_available_false_when_not_recording(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(response.data)["liveAvailable"] is False


def test_live_available_false_when_ffmpeg_mp4(tmp_path, monkeypatch):
    # ffmpeg sites write the .mp4 directly with no .tmp.ts sidecar.
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    streamer.recording = True
    streamer.recording_filename = "alice-20260707-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(response.data)["liveAvailable"] is False


def test_live_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/live")
    assert response.status_code == 401


def test_live_unknown_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/streamers/FakeSite/ghost/live", headers=AUTH)
    assert response.status_code == 404


def test_live_not_recording_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/live", headers=AUTH)
    assert response.status_code == 404


def test_live_missing_tmp_file_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    streamer.recording = True
    streamer.recording_filename = "alice-20260707-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/live", headers=AUTH)
    assert response.status_code == 404


def test_live_streams_ts_aligned_to_packet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 100 whole TS packets; each begins with the 0x47 sync byte.
    packets = 100
    ts_data = b"".join(b"\x47" + bytes([i % 256]) * 187 for i in range(packets))
    assert len(ts_data) == packets * 188

    streamer = make_streamer()
    _start_recording(streamer, ts_data=ts_data)
    # Force a tail backoff that lands mid-packet so alignment is actually tested,
    # and stop tailing after the first empty read.
    monkeypatch.setattr(api_v2, "_LIVE_TAIL_BYTES", 1000)
    monkeypatch.setattr(api_v2, "_LIVE_MAX_IDLE_ITERS", 1)
    monkeypatch.setattr(api_v2, "_LIVE_POLL_INTERVAL", 0)

    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/live", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "video/mp2t"
    assert response.headers["X-Accel-Buffering"] == "no"
    body = response.data
    assert len(body) > 0
    assert len(body) % 188 == 0          # ends on a packet boundary
    assert body[0:1] == b"\x47"          # starts aligned on the sync byte
    # Start = (size - tail) rounded down to a packet boundary.
    expected_start = (len(ts_data) - 1000)
    expected_start -= expected_start % 188
    assert body == ts_data[expected_start:]


def _fmp4_box(box_type, payload=b""):
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _make_fmp4(fragments=5, frag_payload=b"X" * 100):
    # Minimal fragmented-MP4: init (ftyp + moov) followed by moof/mdat pairs.
    init = _fmp4_box(b"ftyp", b"iso5" + b"\x00" * 4) + _fmp4_box(b"moov", b"\x00" * 40)
    body = b""
    for i in range(fragments):
        body += _fmp4_box(b"moof", bytes([i]) * 16) + _fmp4_box(b"mdat", frag_payload + bytes([i]))
    return init, body


def test_live_streams_fmp4_prepends_init_and_tails(tmp_path, monkeypatch):
    # StripChat/CMAF writes fMP4 into the .tmp.ts; the endpoint must serve
    # video/mp4, prepend the init segment, and tail from a recent moof boundary.
    monkeypatch.chdir(tmp_path)
    init, body = _make_fmp4(fragments=5)
    data = init + body

    streamer = make_streamer()
    _start_recording(streamer, ts_data=data)
    # Small tail so early fragments are trimmed and backoff is exercised.
    monkeypatch.setattr(api_v2, "_LIVE_TAIL_BYTES", 200)
    monkeypatch.setattr(api_v2, "_LIVE_MAX_IDLE_ITERS", 1)
    monkeypatch.setattr(api_v2, "_LIVE_POLL_INTERVAL", 0)

    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/live", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "video/mp4"
    assert response.headers["X-Accel-Buffering"] == "no"
    out = response.data
    assert out.startswith(init)                       # init segment prepended
    tail = out[len(init):]
    assert tail[4:8] == b"moof"                        # tail starts at a fragment
    assert out.endswith(data[-8:])                     # includes the newest bytes
    assert len(out) < len(init) + len(data)           # backoff trimmed early frags


# --- settings: server-side download directory (NB4) ---

@pytest.fixture
def preserve_download_dir(monkeypatch):
    # set_download_dir mutates the module global; snapshot so tests never leak
    # a changed directory into the rest of the suite or the real process.
    monkeypatch.setattr(parameters, "DOWNLOADS_DIR", parameters.DOWNLOADS_DIR)
    yield


def test_get_download_dir_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/download-dir")
    assert response.status_code == 401


def test_get_download_dir_returns_abspath(tmp_path, monkeypatch, preserve_download_dir):
    monkeypatch.chdir(tmp_path)
    parameters.set_download_dir(str(tmp_path / "dls"))
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/download-dir", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["path"] == str(tmp_path / "dls")
    assert os.path.isabs(payload["path"])
    for key in ("total", "free", "used", "freeHuman", "totalHuman"):
        assert key in payload["diskSpace"]


def test_set_download_dir_valid_persists(tmp_path, monkeypatch, preserve_download_dir):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "new-downloads"
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/download-dir", json={"path": str(target)}, headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["path"] == str(target)
    assert target.is_dir()                      # created on demand
    assert parameters.get_download_dir() == str(target)   # runtime effect
    env_text = (tmp_path / ".env").read_text()   # persisted for restart
    assert f"STRMNTR_DOWNLOAD_DIR={target}" in env_text


def test_set_download_dir_accepts_post(tmp_path, monkeypatch, preserve_download_dir):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "via-post"
    manager = HTTPManager([])
    response = _client(manager).post(
        "/api/v2/settings/download-dir", json={"path": str(target)}, headers=AUTH)
    assert response.status_code == 200
    assert parameters.get_download_dir() == str(target)


def test_set_download_dir_empty_400(tmp_path, monkeypatch, preserve_download_dir):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/download-dir", json={"path": "   "}, headers=AUTH)
    assert response.status_code == 400
    assert json.loads(response.data)["code"] == "empty"


def test_set_download_dir_illegal_path_400(tmp_path, monkeypatch, preserve_download_dir):
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    target = blocker / "sub"   # child of a regular file — cannot be created
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/download-dir", json={"path": str(target)}, headers=AUTH)
    assert response.status_code == 400
    assert json.loads(response.data)["code"] == "create_failed"
    assert parameters.get_download_dir() != str(target)   # rejected, no change


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses filesystem write permissions",
)
def test_set_download_dir_unwritable_400(tmp_path, monkeypatch, preserve_download_dir):
    monkeypatch.chdir(tmp_path)
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    os.chmod(readonly, 0o500)
    manager = HTTPManager([])
    try:
        response = _client(manager).put(
            "/api/v2/settings/download-dir", json={"path": str(readonly)}, headers=AUTH)
        assert response.status_code == 400
        assert json.loads(response.data)["code"] == "not_writable"
    finally:
        os.chmod(readonly, 0o700)   # let pytest clean up tmp_path


# --- settings: NSFW analysis runtime toggle ---

@pytest.fixture
def preserve_nsfw(monkeypatch):
    # The toggle mutates parameters globals and the analyzer's session cache;
    # snapshot both so tests never leak state into the rest of the suite.
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    monkeypatch.setattr(parameters, "NSFW_ENABLED", parameters.NSFW_ENABLED)
    monkeypatch.setattr(parameters, "NSFW_ACTION", parameters.NSFW_ACTION)
    monkeypatch.setattr(parameters, "NSFW_RECYCLE_RETENTION_DAYS", parameters.NSFW_RECYCLE_RETENTION_DAYS)
    monkeypatch.setattr(parameters, "NSFW_SCAN_INTERVAL_HOURS", parameters.NSFW_SCAN_INTERVAL_HOURS)
    monkeypatch.setattr(nsfw_analyzer, "_session", nsfw_analyzer._session)
    monkeypatch.setattr(nsfw_analyzer, "_session_failed", nsfw_analyzer._session_failed)
    yield


def test_get_nsfw_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/nsfw")
    assert response.status_code == 401


def test_get_nsfw_returns_enabled_and_readiness(tmp_path, monkeypatch, preserve_nsfw):
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_enabled(False)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/nsfw", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["enabled"] is False
    assert isinstance(payload["modelPresent"], bool)
    assert isinstance(payload["runtimeAvailable"], bool)
    assert payload["modelPath"] == parameters.NSFW_MODEL_PATH


def test_get_nsfw_broken_runtime_degrades(tmp_path, monkeypatch, preserve_nsfw):
    # A broken installation can make find_spec raise; the endpoint must report
    # runtimeAvailable=false instead of failing with a 500.
    monkeypatch.chdir(tmp_path)
    def boom(name):
        raise ImportError(f"broken installation: {name}")
    monkeypatch.setattr(api_v2.importlib.util, "find_spec", boom)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/nsfw", headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["runtimeAvailable"] is False


def test_set_nsfw_enable_persists(tmp_path, monkeypatch, preserve_nsfw):
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_enabled(False)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/nsfw", json={"enabled": True}, headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is True
    assert parameters.get_nsfw_enabled() is True          # runtime effect
    env_text = (tmp_path / ".env").read_text()             # persisted for restart
    assert "STRMNTR_NSFW_ENABLED=true" in env_text


def test_set_nsfw_disable_unloads_session(tmp_path, monkeypatch, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_enabled(True)
    nsfw_analyzer._session = object()          # pretend a model is loaded
    nsfw_analyzer._session_failed = False
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/nsfw", json={"enabled": False}, headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is False
    assert parameters.get_nsfw_enabled() is False
    assert nsfw_analyzer._session is None                  # model released
    assert nsfw_analyzer._session_failed is False          # failure latch reset
    env_text = (tmp_path / ".env").read_text()
    assert "STRMNTR_NSFW_ENABLED=false" in env_text


def test_set_nsfw_invalid_400_no_change(tmp_path, monkeypatch, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_enabled(True)
    sentinel = object()
    nsfw_analyzer._session = sentinel
    manager = HTTPManager([])
    for bad in ("yes", 1, None, [True]):
        response = _client(manager).put(
            "/api/v2/settings/nsfw", json={"enabled": bad}, headers=AUTH)
        assert response.status_code == 400
        assert json.loads(response.data)["code"] == "invalid"
    assert parameters.get_nsfw_enabled() is True           # state unchanged
    assert nsfw_analyzer._session is sentinel              # session untouched
    assert not (tmp_path / ".env").exists()                # nothing persisted


def test_get_nsfw_returns_handling_config(tmp_path, monkeypatch, preserve_nsfw):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/nsfw", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["action"] in parameters.NSFW_ACTIONS
    assert 1 <= payload["recycleRetentionDays"] <= 90
    assert 0 <= payload["scanIntervalHours"] <= 168


def test_set_nsfw_handling_config_persists(tmp_path, monkeypatch, preserve_nsfw):
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_enabled(False)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/nsfw",
        json={"enabled": True, "action": "recycle",
              "recycleRetentionDays": 14, "scanIntervalHours": 12},
        headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["action"] == "recycle"
    assert payload["recycleRetentionDays"] == 14
    assert payload["scanIntervalHours"] == 12
    assert parameters.get_nsfw_action() == "recycle"       # runtime effect
    assert parameters.get_nsfw_recycle_retention_days() == 14
    assert parameters.get_nsfw_scan_interval_hours() == 12
    env_text = (tmp_path / ".env").read_text()             # persisted for restart
    assert "STRMNTR_NSFW_ACTION=recycle" in env_text
    assert "STRMNTR_NSFW_RECYCLE_RETENTION_DAYS=14" in env_text
    assert "STRMNTR_NSFW_SCAN_INTERVAL_HOURS=12" in env_text


def test_set_nsfw_enabled_only_keeps_handling_config(tmp_path, monkeypatch, preserve_nsfw):
    # Old clients send only {enabled}; the sub-options must stay untouched.
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_action("recycle")
    parameters.set_nsfw_recycle_retention_days(30)
    parameters.set_nsfw_scan_interval_hours(24)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/nsfw", json={"enabled": True}, headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["action"] == "recycle"
    assert payload["recycleRetentionDays"] == 30
    assert payload["scanIntervalHours"] == 24


def test_set_nsfw_handling_invalid_400_no_change(tmp_path, monkeypatch, preserve_nsfw):
    monkeypatch.chdir(tmp_path)
    parameters.set_nsfw_enabled(False)
    parameters.set_nsfw_action("mark_only")
    parameters.set_nsfw_recycle_retention_days(7)
    parameters.set_nsfw_scan_interval_hours(6)
    manager = HTTPManager([])
    bad_payloads = [
        {"enabled": True, "action": "shred"},
        {"enabled": True, "recycleRetentionDays": 0},
        {"enabled": True, "recycleRetentionDays": 91},
        {"enabled": True, "recycleRetentionDays": 7.5},
        {"enabled": True, "recycleRetentionDays": True},
        {"enabled": True, "scanIntervalHours": -1},
        {"enabled": True, "scanIntervalHours": 169},
        {"enabled": True, "scanIntervalHours": "6"},
    ]
    for bad in bad_payloads:
        response = _client(manager).put(
            "/api/v2/settings/nsfw", json=bad, headers=AUTH)
        assert response.status_code == 400, bad
        assert json.loads(response.data)["code"] == "invalid"
    assert parameters.get_nsfw_enabled() is False          # nothing applied
    assert parameters.get_nsfw_action() == "mark_only"
    assert parameters.get_nsfw_recycle_retention_days() == 7
    assert parameters.get_nsfw_scan_interval_hours() == 6
    assert not (tmp_path / ".env").exists()                # nothing persisted


# --- NSFW analyzer: runtime load/unload regression ---

def test_nsfw_get_session_noop_while_disabled(preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    parameters.set_nsfw_enabled(False)
    nsfw_analyzer._session = None
    nsfw_analyzer._session_failed = False
    assert nsfw_analyzer._get_session() is None
    assert nsfw_analyzer._session is None                  # nothing was loaded
    assert nsfw_analyzer._session_failed is False          # and nothing failed


def test_nsfw_unload_resets_failure_latch(tmp_path, monkeypatch, preserve_nsfw):
    # A failed load (missing model file) latches; unload_session resets the
    # latch so the next enable retries loading instead of staying degraded.
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    monkeypatch.setattr(parameters, "NSFW_MODEL_PATH", str(tmp_path / "missing.onnx"))
    parameters.set_nsfw_enabled(True)
    nsfw_analyzer._session = None
    nsfw_analyzer._session_failed = False
    assert nsfw_analyzer._get_session() is None
    assert nsfw_analyzer._session_failed is True           # load failed, latched
    nsfw_analyzer.unload_session()
    assert nsfw_analyzer._session_failed is False
    assert nsfw_analyzer._get_session() is None            # retried (and failed again)
    assert nsfw_analyzer._session_failed is True


def _fake_bot(tmp_path, username="alice"):
    from types import SimpleNamespace
    return SimpleNamespace(
        username=username,
        outputFolder=str(tmp_path),
        recording_filename=f"{username}-20260101-000000.mp4",
    )


def test_nsfw_monitor_thread_exits_when_disabled(tmp_path, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    parameters.set_nsfw_enabled(True)
    monitor = nsfw_analyzer.RecordingMonitor(_fake_bot(tmp_path))
    parameters.set_nsfw_enabled(False)
    sampled = []
    monitor._sample_once = lambda: sampled.append(1)
    monitor._stop.wait = lambda timeout=None: False   # skip the interval sleep
    monitor._run()                                    # returns instead of sampling
    assert sampled == []


def test_nsfw_finalize_abandons_when_disabled(tmp_path, monkeypatch, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    import streamonitor.media_index as media_index
    parameters.set_nsfw_enabled(True)
    monitor = nsfw_analyzer.RecordingMonitor(_fake_bot(tmp_path))
    (tmp_path / f"{monitor.base}.mp4").write_bytes(b"x")
    monitor.scores = [0.9]
    monitor.adult_seen = True
    written = []
    monkeypatch.setattr(media_index, "set_classification",
                        lambda path, label: written.append((path, label)))
    parameters.set_nsfw_enabled(False)   # toggle off mid-recording
    monitor._stop.set()
    monitor._finalize()
    assert written == []                 # partial classification abandoned


def test_nsfw_finalize_writes_when_enabled(tmp_path, monkeypatch, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    import streamonitor.media_index as media_index
    parameters.set_nsfw_enabled(True)
    monitor = nsfw_analyzer.RecordingMonitor(_fake_bot(tmp_path))
    final = tmp_path / f"{monitor.base}.mp4"
    final.write_bytes(b"x")
    monitor.scores = [0.9]
    monitor.adult_seen = True            # skips the fallback sampling
    written = []
    monkeypatch.setattr(media_index, "set_classification",
                        lambda path, label: written.append((path, label)))
    monitor._stop.set()
    monitor._finalize()
    assert written == [(str(final), "adult")]


def test_nsfw_start_monitor_noop_when_disabled(tmp_path, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    parameters.set_nsfw_enabled(False)
    bot = _fake_bot(tmp_path)
    nsfw_analyzer.start_monitor(bot)
    assert getattr(bot, "_nsfw_monitor", None) is None


def test_nsfw_start_monitor_attaches_when_enabled(tmp_path, preserve_nsfw):
    import streamonitor.nsfw_analyzer as nsfw_analyzer
    parameters.set_nsfw_enabled(True)
    bot = _fake_bot(tmp_path)
    nsfw_analyzer.start_monitor(bot)
    monitor = getattr(bot, "_nsfw_monitor", None)
    assert monitor is not None
    assert monitor._thread.is_alive()
    monitor._stop.set()                  # stop the sampling thread right away



# --- settings: recording resolution runtime setting (V33A) ---

@pytest.fixture
def preserve_recording_resolution(monkeypatch):
    # set_recording_resolution mutates module globals; snapshot so tests never
    # leak a changed value into the rest of the suite or the real process.
    monkeypatch.setattr(
        parameters, "_RECORDING_RESOLUTION", parameters._RECORDING_RESOLUTION)
    monkeypatch.setattr(
        parameters, "_RECORDING_RESOLUTION_PREFERENCE",
        parameters._RECORDING_RESOLUTION_PREFERENCE)
    yield


def test_get_recording_resolution_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/settings/recording-resolution")
    assert response.status_code == 401


def test_get_recording_resolution_default_is_max(
        tmp_path, monkeypatch, preserve_recording_resolution):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRMNTR_RESOLUTION", raising=False)
    monkeypatch.delenv("STRMNTR_RESOLUTION_PREF", raising=False)
    # Simulate a clean start: no persistence file and no env override.
    monkeypatch.setattr(
        parameters, "_RECORDING_RESOLUTION",
        parameters._initial_recording_resolution())
    monkeypatch.setattr(
        parameters, "_RECORDING_RESOLUTION_PREFERENCE",
        parameters._initial_recording_resolution_preference())
    manager = HTTPManager([])
    response = _client(manager).get(
        "/api/v2/settings/recording-resolution", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["resolution"] == "max"
    assert payload["preference"] == "closest"


def test_set_recording_resolution_int_persists(
        tmp_path, monkeypatch, preserve_recording_resolution):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/recording-resolution",
        json={"resolution": 720, "preference": "exact_or_highest_lower"},
        headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["resolution"] == 720
    assert payload["preference"] == "exact_or_highest_lower"
    assert parameters.get_recording_resolution() == 720            # runtime effect
    assert parameters.get_recording_resolution_preference() == "exact_or_highest_lower"
    stored = json.loads(
        (tmp_path / parameters.RECORDING_RESOLUTION_STORE).read_text())
    assert stored == {"resolution": 720, "preference": "exact_or_highest_lower"}


def test_set_recording_resolution_max_persists(
        tmp_path, monkeypatch, preserve_recording_resolution):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/recording-resolution",
        json={"resolution": "max"}, headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["resolution"] == "max"
    assert parameters.get_recording_resolution() == "max"


def test_set_recording_resolution_accepts_post(
        tmp_path, monkeypatch, preserve_recording_resolution):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).post(
        "/api/v2/settings/recording-resolution",
        json={"resolution": 1080}, headers=AUTH)
    assert response.status_code == 200
    assert parameters.get_recording_resolution() == 1080


def test_set_recording_resolution_missing_400(
        tmp_path, monkeypatch, preserve_recording_resolution):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/recording-resolution", json={}, headers=AUTH)
    assert response.status_code == 400
    assert json.loads(response.data)["code"] == "missing"


@pytest.mark.parametrize("value", [99, 10000, "abc", 720.5, True])
def test_set_recording_resolution_invalid_resolution_400(
        tmp_path, monkeypatch, preserve_recording_resolution, value):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/recording-resolution",
        json={"resolution": value}, headers=AUTH)
    assert response.status_code == 400
    assert json.loads(response.data)["code"] == "invalid_resolution"


def test_set_recording_resolution_invalid_preference_400(
        tmp_path, monkeypatch, preserve_recording_resolution):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).put(
        "/api/v2/settings/recording-resolution",
        json={"resolution": 720, "preference": "bogus"}, headers=AUTH)
    assert response.status_code == 400
    assert json.loads(response.data)["code"] == "invalid_preference"


def test_recording_resolution_persists_across_reload(
        tmp_path, monkeypatch, preserve_recording_resolution):
    # Simulate a restart: PUT persists, then a fresh load_recording_resolution()
    # re-reads the store and the value survives.
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    _client(manager).put(
        "/api/v2/settings/recording-resolution",
        json={"resolution": 720, "preference": "closest"}, headers=AUTH)
    monkeypatch.setattr(parameters, "_RECORDING_RESOLUTION", "max")
    monkeypatch.setattr(parameters, "_RECORDING_RESOLUTION_PREFERENCE", "exact")
    parameters.load_recording_resolution()
    assert parameters.get_recording_resolution() == 720
    assert parameters.get_recording_resolution_preference() == "closest"


def test_persisted_resolution_takes_priority_over_env(
        tmp_path, monkeypatch, preserve_recording_resolution):
    # The env var is only the first-run initial value; a persisted store wins.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRMNTR_RESOLUTION", "1080")
    parameters.persist_recording_resolution(480, "closest")
    monkeypatch.setattr(
        parameters, "_RECORDING_RESOLUTION",
        parameters._initial_recording_resolution())
    parameters.load_recording_resolution()
    assert parameters.get_recording_resolution() == 480


# --- video thumbnails + duration (NB6) ---

FFMPEG_AVAILABLE = (
    shutil.which(parameters.FFMPEG_PATH) is not None and shutil.which("ffprobe") is not None
)


def _make_real_video(streamer, filename="alice-20260706-130000.mp4", seconds=2):
    os.makedirs(streamer.outputFolder, exist_ok=True)
    path = os.path.join(streamer.outputFolder, filename)
    subprocess.run(
        [parameters.FFMPEG_PATH, "-y", "-f", "lavfi",
         "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    streamer.cache_file_list()
    return path


def test_video_thumb_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-120000.mp4/thumb")
    assert response.status_code == 401


def test_video_thumb_unknown_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/ghost/videos/x.mp4/thumb", headers=AUTH)
    assert response.status_code == 404


def test_video_thumb_missing_file_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/videos/nope.mp4/thumb", headers=AUTH)
    assert response.status_code == 404


def test_video_thumb_broken_file_404(tmp_path, monkeypatch):
    # A file that ffmpeg cannot decode returns 404 so the frontend falls back to
    # its generated placeholder image.
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-120000.mp4/thumb",
        headers=AUTH)
    assert response.status_code == 404


def test_video_thumb_recording_in_progress_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    streamer.recording = True
    streamer.recording_filename = "alice-20260706-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-120000.mp4/thumb",
        headers=AUTH)
    assert response.status_code == 404


def test_detail_includes_duration_field(tmp_path, monkeypatch):
    # The duration key is always present. Reads are cache-only, so an uncached
    # file reports null immediately and is queued for the background worker.
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    monkeypatch.setattr(api_v2, "_probe_duration", lambda p: None)
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert response.status_code == 200
    video = json.loads(response.data)["videos"][0]
    assert "duration" in video
    assert video["duration"] is None
    api_v2._probe_queue.join()


def test_detail_recording_video_has_null_duration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    streamer.recording = True
    streamer.recording_filename = "alice-20260706-120000.mp4"
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    video = json.loads(response.data)["videos"][0]
    assert video["recording"] is True
    assert video["duration"] is None


def test_detail_classification_null_and_not_in_recycle_by_default(tmp_path, monkeypatch):
    # Disk-scan fallback (no media index db): the keys are always present,
    # classification is null (not analyzed) and inRecycle is false.
    monkeypatch.chdir(tmp_path)
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    video = json.loads(response.data)["videos"][0]
    assert video["classification"] is None
    assert video["inRecycle"] is False
    api_v2._probe_queue.join()


def test_detail_classification_comes_from_media_index(tmp_path, monkeypatch):
    import streamonitor.media_index as media_index
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 0)
    streamer, path = _make_streamer_with_video()
    media_index.scan_once()
    media_index.set_classification(path, "adult")
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    video = json.loads(response.data)["videos"][0]
    assert video["classification"] == "adult"
    assert video["inRecycle"] is False
    api_v2._probe_queue.join()


def test_detail_flags_video_in_recycle_bin(tmp_path, monkeypatch):
    # An index row whose abs_path lives under downloads/_recycle is reported
    # with inRecycle=true so the UI can gray it out.
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    recycle_dir = tmp_path / "downloads" / "_recycle" / "alice [FS]"
    recycle_dir.mkdir(parents=True)
    fpath = recycle_dir / "alice-20260706-120000.mp4"
    fpath.write_bytes(b"0" * 1000)
    rows = [{"filename": "alice-20260706-120000.mp4", "abs_path": str(fpath),
             "size": 1000, "mtime": 1_700_000_000, "duration": 12.0}]
    monkeypatch.setattr(api_v2.media_index, "list_videos", lambda site, user: rows)
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    video = json.loads(response.data)["videos"][0]
    assert video["inRecycle"] is True
    assert video["classification"] is None


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
def test_video_thumb_real_video_generates_and_caches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    _make_real_video(streamer)
    manager = HTTPManager([streamer])
    client = _client(manager)

    response = client.get(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-130000.mp4/thumb",
        headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert len(response.data) > 0
    assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"

    video = streamer.video_files[0]
    thumb_path = api_v2._thumb_cache_path(video.abs_path, int(video._stat.st_mtime))
    assert os.path.exists(thumb_path)
    first_mtime = os.path.getmtime(thumb_path)

    # Second request must serve the cached file without regenerating it.
    again = client.get(
        "/api/v2/streamers/FakeSite/alice/videos/alice-20260706-130000.mp4/thumb",
        headers=AUTH)
    assert again.status_code == 200
    assert os.path.getmtime(thumb_path) == first_mtime


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
def test_detail_duration_real_video(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    streamer = make_streamer()
    _make_real_video(streamer, seconds=2)
    manager = HTTPManager([streamer])
    # First read is cache-only (null) and queues a background probe.
    first = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(first.data)["videos"][0]["duration"] is None
    api_v2._probe_queue.join()
    # After the worker probes, the warm cache serves the real duration.
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    video = json.loads(response.data)["videos"][0]
    assert video["duration"] is not None
    assert 1.0 < video["duration"] < 4.0


def _reset_durations_cache(monkeypatch):
    # Drop the in-memory cache so a test starts cold (also simulates a restart).
    monkeypatch.setattr(api_v2, "_durations_cache", None)
    monkeypatch.setattr(api_v2, "_durations_cache_path", None)


def test_duration_cache_persists_to_consolidated_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    calls = []
    monkeypatch.setattr(api_v2, "_probe_duration", lambda p: (calls.append(p) or 123.0))
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    # Reads are non-blocking: the first request returns null and queues a probe.
    first = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(first.data)["videos"][0]["duration"] is None
    api_v2._probe_queue.join()
    assert len(calls) == 1
    # The probed duration is now served from the warm cache and persisted to disk.
    second = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(second.data)["videos"][0]["duration"] == 123.0
    dpath = api_v2._durations_path()
    assert os.path.exists(dpath)
    with open(dpath) as f:
        stored = json.load(f)
    assert 123.0 in stored.values()


def test_duration_cache_survives_restart_without_reprobe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    calls = []
    monkeypatch.setattr(api_v2, "_probe_duration", lambda p: (calls.append(p) or 42.0))
    streamer, _ = _make_streamer_with_video()
    _client(HTTPManager([streamer])).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    api_v2._probe_queue.join()
    assert len(calls) == 1
    # Simulate a service restart: forget the in-memory cache, keep durations.json.
    _reset_durations_cache(monkeypatch)
    streamer2, _ = _make_streamer_with_video()
    resp = _client(HTTPManager([streamer2])).get(
        "/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(resp.data)["videos"][0]["duration"] == 42.0
    api_v2._probe_queue.join()
    assert len(calls) == 1   # cache hit from disk, no second ffprobe


def test_duration_cache_caches_failed_probe_as_null(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    calls = []
    monkeypatch.setattr(api_v2, "_probe_duration", lambda p: (calls.append(p), None)[1])
    streamer, _ = _make_streamer_with_video()
    _client(HTTPManager([streamer])).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    api_v2._probe_queue.join()
    assert len(calls) == 1
    _reset_durations_cache(monkeypatch)
    resp = _client(HTTPManager([streamer])).get(
        "/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(resp.data)["videos"][0]["duration"] is None
    api_v2._probe_queue.join()
    assert len(calls) == 1   # a failed probe is cached and never retried


def test_duration_cache_tolerates_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    monkeypatch.setattr(api_v2, "_probe_duration", lambda p: 7.0)
    os.makedirs(api_v2._thumbs_dir(), exist_ok=True)
    with open(api_v2._durations_path(), "w") as f:
        f.write("{ not valid json ")
    streamer, _ = _make_streamer_with_video()
    manager = HTTPManager([streamer])
    # A corrupt cache file is treated as empty: the first read is a null miss
    # that queues a probe; after the worker runs the duration is served.
    first = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert first.status_code == 200
    assert json.loads(first.data)["videos"][0]["duration"] is None
    api_v2._probe_queue.join()
    resp = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(resp.data)["videos"][0]["duration"] == 7.0


def test_prune_durations_removes_only_stale_keys(tmp_path, monkeypatch):
    # Direct unit test of the sweep primitive: keys absent from the live set are
    # dropped, live keys survive, and the atomic rewrite reuses the same file.
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    os.makedirs(api_v2._thumbs_dir(), exist_ok=True)
    with open(api_v2._durations_path(), "w") as f:
        json.dump({"keep": 10.0, "stale": 20.0}, f)
    removed = api_v2._prune_durations({"keep"})
    assert removed == 1
    with open(api_v2._durations_path()) as f:
        assert json.load(f) == {"keep": 10.0}


def test_duration_cleanup_drops_stale_entry_on_detail(tmp_path, monkeypatch):
    # A key for a file that no longer exists is removed once the sweep triggers,
    # while the entry for the live video is preserved. Threshold/growth are lowered
    # so the gated sweep fires for a tiny cache.
    monkeypatch.chdir(tmp_path)
    _reset_durations_cache(monkeypatch)
    monkeypatch.setattr(api_v2, "_probe_duration", lambda p: 55.0)
    monkeypatch.setattr(api_v2, "_DURATIONS_MAX_KEYS", 0)
    monkeypatch.setattr(api_v2, "_DURATIONS_CLEANUP_GROWTH", 0)
    monkeypatch.setattr(api_v2, "_durations_last_sweep_size", 0)
    streamer, path = _make_streamer_with_video()
    manager = HTTPManager([streamer])

    _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    live_key = api_v2._cache_key(
        streamer.video_files[0].abs_path, int(streamer.video_files[0]._stat.st_mtime))
    # The first request queued the probe; wait for the worker, then confirm value.
    api_v2._probe_queue.join()
    first = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(first.data)["videos"][0]["duration"] == 55.0

    # Inject a stale key (no matching file) and force the next request to re-sweep.
    stale_key = "0" * 40
    with api_v2._durations_lock:
        api_v2._durations_cache[stale_key] = 99.0
    monkeypatch.setattr(api_v2, "_durations_last_sweep_size", 0)

    second = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert json.loads(second.data)["videos"][0]["duration"] == 55.0
    with open(api_v2._durations_path()) as f:
        stored = json.load(f)
    assert stale_key not in stored
    assert stored.get(live_key) == 55.0


# --- model avatar: real on-site avatar with proxy download + cache (NB12) ---

class _FakeResp:
    def __init__(self, content=b"", status_code=200, content_type="image/png"):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_avatar_extractor_stripchat():
    assert api_v2._avatar_from_stripchat({"model": {"avatarUrl": "u1"}}) == "u1"
    assert api_v2._avatar_from_stripchat({"model": {"previewUrl": "u2"}}) == "u2"
    assert api_v2._avatar_from_stripchat({"model": {}}) is None


def test_avatar_extractor_chaturbate():
    assert api_v2._avatar_from_chaturbate({"img": "cb"}) == "cb"
    assert api_v2._avatar_from_chaturbate({}) is None


def test_avatar_source_url_unknown_site_returns_none():
    streamer = make_streamer()  # FakeSite, siteslug "FS" — no extractor registered
    streamer.lastInfo = {"avatarUrl": "whatever"}
    assert api_v2._avatar_source_url(streamer) is None


def test_avatar_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/avatar")
    assert response.status_code == 401


def test_avatar_unknown_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/ghost/avatar", headers=AUTH)
    assert response.status_code == 404


def test_avatar_no_channel_404(tmp_path, monkeypatch):
    # Unknown site (no extractor) and no prior cache -> 404 so the frontend falls
    # back to its generated placeholder.
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/avatar", headers=AUTH)
    assert response.status_code == 404


def test_avatar_downloads_then_serves_from_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    streamer.lastInfo = {"avatarUrl": "https://example/av.png"}
    calls = []
    streamer.session.get = lambda url, **kw: (calls.append(url) or _FakeResp(_PNG_BYTES))
    # Register a temporary extractor for the fake site's slug.
    monkeypatch.setitem(api_v2._AVATAR_EXTRACTORS, "FS", lambda info: info.get("avatarUrl"))
    manager = HTTPManager([streamer])
    client = _client(manager)

    first = client.get("/api/v2/streamers/FakeSite/alice/avatar", headers=AUTH)
    assert first.status_code == 200
    assert first.mimetype == "image/png"
    assert first.data == _PNG_BYTES
    assert first.headers["Cache-Control"] == "private, max-age=3600"
    assert len(calls) == 1
    cache_path = api_v2._avatar_cache_path(streamer.site, streamer.username)
    assert os.path.exists(cache_path)

    # Second request within TTL serves the cached file without re-downloading.
    second = client.get("/api/v2/streamers/FakeSite/alice/avatar", headers=AUTH)
    assert second.status_code == 200
    assert second.data == _PNG_BYTES
    assert len(calls) == 1


def test_avatar_offline_serves_stale_cache(tmp_path, monkeypatch):
    # lastInfo empty (offline) but a cached avatar exists -> serve the cache.
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    streamer.lastInfo = {}
    cache_path = api_v2._avatar_cache_path(streamer.site, streamer.username)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(_PNG_BYTES)
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/avatar", headers=AUTH)
    assert response.status_code == 200
    assert response.data == _PNG_BYTES


# --- site favicon: real site icon with fetch + cache (NB11) ---

def test_site_favicon_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/sites/FS/favicon")
    assert response.status_code == 401


def test_site_favicon_unknown_site_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/sites/ZZ/favicon", headers=AUTH)
    assert response.status_code == 404
    assert json.loads(response.data)["error"] == "Unknown site"


def test_site_favicon_fetch_failure_404(tmp_path, monkeypatch, fake_site):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_v2, "_fetch_favicon", lambda domain: None)
    manager = HTTPManager([])
    response = _client(manager).get("/api/v2/sites/FS/favicon", headers=AUTH)
    assert response.status_code == 404
    assert json.loads(response.data)["error"] == "Favicon unavailable"


def test_site_favicon_fetches_and_caches(tmp_path, monkeypatch, fake_site):
    monkeypatch.chdir(tmp_path)
    calls = []

    def fake_fetch(domain):
        calls.append(domain)
        return _PNG_BYTES

    monkeypatch.setattr(api_v2, "_fetch_favicon", fake_fetch)
    manager = HTTPManager([])
    client = _client(manager)

    response = client.get("/api/v2/sites/FS/favicon", headers=AUTH)
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == _PNG_BYTES
    assert response.headers["Cache-Control"] == "private, max-age=86400"
    assert calls == ["fake"]

    cache_path = api_v2._favicon_cache_path("FS")
    assert os.path.exists(cache_path)

    # Second request must serve the cached file without fetching again.
    again = client.get("/api/v2/sites/FS/favicon", headers=AUTH)
    assert again.status_code == 200
    assert again.data == _PNG_BYTES
    assert calls == ["fake"]


# --- live recording speed + resolution on the list card (V31D) ---

def test_streamers_list_recording_metrics_null_when_not_recording(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()   # recording defaults to False
    streamer.recording_resolution = 1080
    streamer.recording_speed_bps = 2.5 * 1024 * 1024
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers", headers=AUTH)
    assert response.status_code == 200
    item = json.loads(response.data)[0]
    assert item["recordingSpeedBps"] is None
    assert item["recordingResolution"] is None


def test_detail_recording_metrics_exposed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    streamer.recording = True
    streamer.recording_resolution = 1080
    streamer.recording_speed_bps = 2.5 * 1024 * 1024
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["recordingResolution"] == 1080
    assert payload["recordingSpeedBps"] == 2.5 * 1024 * 1024


def test_recording_resolution_null_when_unknown(tmp_path, monkeypatch):
    # Sites that never resolve a resolution (e.g. non-HLS) report null even while
    # recording, so the frontend degrades to showing nothing.
    monkeypatch.chdir(tmp_path)
    streamer = make_streamer()
    streamer.recording = True
    streamer.recording_resolution = None
    streamer.recording_speed_bps = None
    manager = HTTPManager([streamer])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice", headers=AUTH)
    payload = json.loads(response.data)
    assert payload["recordingResolution"] is None
    assert payload["recordingSpeedBps"] is None


def test_update_recording_progress_computes_rolling_speed(monkeypatch):
    streamer = make_streamer()
    clock = {"t": 1000.0}
    monkeypatch.setattr("streamonitor.bot.time.time", lambda: clock["t"])

    streamer.update_recording_progress(0)
    assert streamer.recording_speed_bps is None   # single sample — not enough

    clock["t"] = 1002.0
    streamer.update_recording_progress(2 * 1024 * 1024)   # +2 MB over 2 s
    assert streamer.recording_bytes == 2 * 1024 * 1024
    assert streamer.recording_speed_bps == pytest.approx(1024 * 1024)


def test_reset_recording_progress_clears_metrics(monkeypatch):
    streamer = make_streamer()
    clock = {"t": 5000.0}
    monkeypatch.setattr("streamonitor.bot.time.time", lambda: clock["t"])
    streamer.update_recording_progress(1000)
    clock["t"] = 5001.0
    streamer.update_recording_progress(5000)
    streamer.recording_resolution = 720
    assert streamer.recording_speed_bps is not None

    streamer.reset_recording_progress()
    assert streamer.recording_bytes == 0
    assert streamer.recording_segments == 0
    assert streamer.recording_resolution is None
    assert streamer.recording_speed_bps is None
    assert streamer._recording_speed_samples == []


# --- online history + schedule API (V36A) ---

import streamonitor.online_history as online_history

_HOUR = 3600
_DAY = 86400


def _reset_online_history(monkeypatch):
    # Forget the in-memory store/dedup/backfill state so each test starts cold
    # (also simulates a service restart). On-disk state is isolated by chdir.
    monkeypatch.setattr(online_history, "_cache", None)
    monkeypatch.setattr(online_history, "_cache_path", None)
    monkeypatch.setattr(online_history, "_last_hour", {})
    monkeypatch.setattr(online_history, "_backfilled", set())


def test_schedule_requires_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get("/api/v2/streamers/FakeSite/alice/schedule")
    assert response.status_code == 401


def test_schedule_unknown_streamer_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = HTTPManager([])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/ghost/schedule", headers=AUTH)
    assert response.status_code == 404


def test_schedule_new_streamer_all_empty(tmp_path, monkeypatch):
    # A brand-new streamer with no observations and no recordings: observedSinceTs
    # null, heat all zero, derived metrics all null (never fabricated).
    monkeypatch.chdir(tmp_path)
    _reset_online_history(monkeypatch)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/schedule", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["observedSinceTs"] is None
    assert payload["sampleDays"] == 0
    assert payload["days7OnlineHours"] is None
    assert payload["avgSessionMin"] is None
    assert payload["commonHours"] is None
    assert len(payload["heat"]) == 7
    assert all(len(row) == 24 for row in payload["heat"])
    assert all(v == 0 for row in payload["heat"] for v in row)


def test_record_online_marks_hour_and_persists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_online_history(monkeypatch)
    ts = 1_700_000_000.0
    online_history.record_online("FakeSite", "alice", ts=ts)
    # Persisted to disk with the expected hour epoch.
    with open(online_history.ONLINE_HISTORY_STORE) as f:
        stored = json.load(f)
    assert stored["FakeSite/alice"] == [int(ts // _HOUR)]


def test_record_online_hourly_dedup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_online_history(monkeypatch)
    base = 1_700_000_000.0
    online_history.record_online("FakeSite", "alice", ts=base)
    online_history.record_online("FakeSite", "alice", ts=base + 60)     # same hour
    online_history.record_online("FakeSite", "alice", ts=base + _HOUR)  # next hour
    with open(online_history.ONLINE_HISTORY_STORE) as f:
        stored = json.load(f)
    assert stored["FakeSite/alice"] == [int(base // _HOUR), int(base // _HOUR) + 1]


def test_schedule_reflects_recorded_observation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_online_history(monkeypatch)
    now = 1_700_000_000.0
    monkeypatch.setattr(online_history, "datetime", _FrozenNow(now))
    online_history.record_online("FakeSite", "alice", ts=now)
    manager = HTTPManager([make_streamer()])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/schedule", headers=AUTH)
    payload = json.loads(response.data)
    assert payload["observedSinceTs"] == int(now // _HOUR) * _HOUR
    assert payload["sampleDays"] == 1
    assert payload["days7OnlineHours"] == 1
    assert sum(v for row in payload["heat"] for v in row) == 1


def test_schedule_persists_across_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _reset_online_history(monkeypatch)
    ts = 1_700_000_000.0
    online_history.record_online("FakeSite", "alice", ts=ts)
    # Simulate a restart: drop the in-memory cache but keep the store on disk.
    _reset_online_history(monkeypatch)
    with online_history._lock:
        online_history._ensure_loaded_locked()
        assert online_history._cache["FakeSite/alice"] == [int(ts // _HOUR)]


def test_schedule_backfills_from_recordings(tmp_path, monkeypatch):
    # A streamer with an existing recording (known duration) returns non-empty heat
    # on the very first query, without any prior polling observation.
    monkeypatch.chdir(tmp_path)
    _reset_online_history(monkeypatch)
    monkeypatch.setattr(api_v2, "_durations_cache", None)
    monkeypatch.setattr(api_v2, "_durations_cache_path", None)
    now = 1_700_000_000.0
    monkeypatch.setattr(online_history, "datetime", _FrozenNow(now))
    streamer, path = _make_streamer_with_video("alice-20260706-120000.mp4")
    # Recording ended "now"; seed the warm durations cache with a 2-hour duration.
    os.utime(path, (now, now))
    streamer.cache_file_list()
    mtime = int(streamer.video_files[0]._stat.st_mtime)
    with api_v2._durations_lock:
        api_v2._load_durations_locked(api_v2._durations_path())
        api_v2._durations_cache[
            api_v2._cache_key(streamer.video_files[0].abs_path, mtime)] = 2 * _HOUR
    manager = HTTPManager([streamer])
    response = _client(manager).get(
        "/api/v2/streamers/FakeSite/alice/schedule", headers=AUTH)
    payload = json.loads(response.data)
    assert payload["observedSinceTs"] is not None
    assert sum(v for row in payload["heat"] for v in row) >= 1


class _FrozenNow:
    # Minimal stand-in for the datetime module: freezes now() but delegates
    # fromtimestamp so bucket math stays real.
    def __init__(self, ts):
        from datetime import datetime as _dt
        self._ts = ts
        self._dt = _dt

    def now(self):
        return self._dt.fromtimestamp(self._ts)

    def fromtimestamp(self, ts):
        return self._dt.fromtimestamp(ts)


# --- settings: web server password ---

@pytest.fixture
def preserve_webserver_password(monkeypatch):
    # set_webserver_password mutates the module global; snapshot so tests never
    # leak a changed password into the rest of the suite.
    monkeypatch.setattr(
        parameters, "WEBSERVER_PASSWORD", parameters.WEBSERVER_PASSWORD)
    monkeypatch.setattr(api_v2, "PASSWORD_FAILURE_DELAY", 0)
    yield


def test_get_password_settings_requires_auth():
    response = _client(HTTPManager([])).get("/api/v2/settings/password")
    assert response.status_code == 401


def test_get_password_settings_never_returns_the_password():
    response = _client(HTTPManager([])).get(
        "/api/v2/settings/password", headers=AUTH)
    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["authEnabled"] is True
    assert payload["minLength"] == parameters.WEBSERVER_PASSWORD_MIN_LENGTH
    assert payload["maxLength"] == parameters.WEBSERVER_PASSWORD_MAX_LENGTH
    assert b"admin" not in response.data


def test_set_password_requires_auth():
    response = _client(HTTPManager([])).put(
        "/api/v2/settings/password",
        json={"currentPassword": "admin", "newPassword": "newpass"})
    assert response.status_code == 401


def test_set_password_success_persists_and_takes_effect(
        tmp_path, monkeypatch, preserve_webserver_password):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("STRMNTR_PORT=5000\nSTRMNTR_PASSWORD=admin\n")
    client = _client(HTTPManager([]))
    response = client.put(
        "/api/v2/settings/password",
        json={"currentPassword": "admin", "newPassword": "newpass"}, headers=AUTH)
    assert response.status_code == 200
    assert json.loads(response.data)["ok"] is True
    assert parameters.get_webserver_password() == "newpass"
    # Other keys survive the atomic rewrite and the new password is stored.
    env_text = (tmp_path / ".env").read_text()
    assert "STRMNTR_PORT=5000" in env_text
    assert 'STRMNTR_PASSWORD="newpass"' in env_text
    assert not (tmp_path / ".env.tmp").exists()
    # No restart needed: the old password stops working immediately.
    assert client.get("/api/v2/streamers", headers=AUTH).status_code == 401
    assert client.get(
        "/api/v2/streamers", headers=basic_auth("admin", "newpass")).status_code == 200


def test_set_password_wrong_current_is_rejected(
        tmp_path, monkeypatch, preserve_webserver_password):
    monkeypatch.chdir(tmp_path)
    response = _client(HTTPManager([])).put(
        "/api/v2/settings/password",
        json={"currentPassword": "nope", "newPassword": "newpass"}, headers=AUTH)
    assert response.status_code == 403
    assert json.loads(response.data)["code"] == "wrong_password"
    assert parameters.get_webserver_password() == "admin"
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("new_password", ["", "abc", "x" * 129, None, 12345])
def test_set_password_rejects_invalid_new_password(
        tmp_path, monkeypatch, preserve_webserver_password, new_password):
    monkeypatch.chdir(tmp_path)
    response = _client(HTTPManager([])).put(
        "/api/v2/settings/password",
        json={"currentPassword": "admin", "newPassword": new_password}, headers=AUTH)
    assert response.status_code == 400
    assert json.loads(response.data)["code"] == "invalid"
    assert parameters.get_webserver_password() == "admin"
    assert not (tmp_path / ".env").exists()


def test_set_password_refused_when_auth_disabled(
        tmp_path, monkeypatch, preserve_webserver_password):
    monkeypatch.chdir(tmp_path)
    parameters.set_webserver_password("")
    response = _client(HTTPManager([])).put(
        "/api/v2/settings/password",
        json={"currentPassword": "", "newPassword": "newpass"})
    assert response.status_code == 409
    assert json.loads(response.data)["code"] == "disabled"


def test_set_password_keeps_current_session_logged_in(
        tmp_path, monkeypatch, preserve_webserver_password):
    monkeypatch.chdir(tmp_path)
    client = _client(HTTPManager([]))
    assert client.post("/login", json={"password": "admin"}).status_code == 200
    response = client.put(
        "/api/v2/settings/password",
        json={"currentPassword": "admin", "newPassword": "newpass"})
    assert response.status_code == 200
    # The session cookie survives the change: no credentials header needed.
    assert client.get("/api/v2/streamers").status_code == 200
