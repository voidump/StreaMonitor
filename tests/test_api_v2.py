import base64
import json
import os

import pytest

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
