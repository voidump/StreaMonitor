import ast
import pathlib

import requests

STREAMONITOR_ROOT = pathlib.Path(__file__).resolve().parent.parent / "streamonitor"


def test_timeout_session_applies_default_timeout(monkeypatch):
    from streamonitor.http_session import TimeoutSession, DEFAULT_TIMEOUT

    captured = {}

    def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    TimeoutSession().get("http://example.invalid/")
    assert captured["timeout"] == DEFAULT_TIMEOUT


def test_timeout_session_keeps_explicit_timeout(monkeypatch):
    from streamonitor.http_session import TimeoutSession

    captured = {}

    def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    TimeoutSession().get("http://example.invalid/", timeout=3)
    assert captured["timeout"] == 3


def test_bot_session_has_default_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from streamonitor.bot import Bot
    from streamonitor.http_session import TimeoutSession

    class FakeSite(Bot):
        site = "FakeSite"
        siteslug = "FAKE"

    bot = FakeSite("someuser")
    assert isinstance(bot.session, TimeoutSession)


def test_no_module_level_requests_call_without_timeout():
    """Every direct requests.get/post/... or local-session call must pass an
    explicit timeout.

    Bot-owned sessions are covered by TimeoutSession's default; this guards
    the module-level requests.* calls and locally created sessions (named
    `session`) that bypass it.
    """
    offenders = []
    for path in STREAMONITOR_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            if func.value.id not in ("requests", "session"):
                continue
            if func.attr not in ("get", "post", "put", "delete", "head", "request"):
                continue
            if not any(kw.arg == "timeout" for kw in node.keywords):
                offenders.append(f"{path.relative_to(STREAMONITOR_ROOT.parent)}:{node.lineno}")
    assert offenders == [], f"HTTP calls without timeout: {offenders}"
