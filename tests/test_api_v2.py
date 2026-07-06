import base64
import json

import pytest

from streamonitor.managers.httpmanager import HTTPManager


def basic_auth(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


AUTH = basic_auth("admin", "admin")


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
