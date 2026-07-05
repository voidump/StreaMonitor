import base64

import pytest

from streamonitor.managers.httpmanager import HTTPManager


def basic_auth(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest.fixture
def client():
    manager = HTTPManager([])
    app = manager.create_app()
    return app.test_client()


def test_video_endpoint_requires_auth(client):
    response = client.get("/video/someuser/SC/recording.mp4")
    assert response.status_code == 401


def test_video_endpoint_rejects_wrong_password(client):
    response = client.get(
        "/video/someuser/SC/recording.mp4",
        headers=basic_auth("admin", "wrong-password"),
    )
    assert response.status_code == 401


def test_video_endpoint_unknown_streamer_returns_404(client):
    response = client.get(
        "/video/someuser/SC/recording.mp4",
        headers=basic_auth("admin", "admin"),
    )
    assert response.status_code == 404
