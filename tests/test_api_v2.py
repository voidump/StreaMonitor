import base64
import json
import os

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
