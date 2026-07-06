import json

import streamonitor.config as config


def test_save_config_writes_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = [{"username": "alice", "site": "StripChat"}]

    assert config.save_config(data) is True
    assert json.loads((tmp_path / "config.json").read_text()) == data


def test_save_config_failure_preserves_existing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = [{"username": "alice", "site": "StripChat"}]
    (tmp_path / "config.json").write_text(json.dumps(original))

    # object() is not JSON serializable, so the dump fails mid-write
    assert config.save_config([object()]) is False
    assert json.loads((tmp_path / "config.json").read_text()) == original


def test_save_config_failure_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("[]")

    config.save_config([object()])
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == []


def test_load_history_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert config.load_history() == []


def test_save_history_writes_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = [{"username": "alice", "site": "StripChat", "deletedAt": 1}]

    assert config.save_history(data) is True
    assert json.loads((tmp_path / "history.json").read_text()) == data


def test_add_history_entry_prepends(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.add_history_entry({"username": "alice"})
    config.add_history_entry({"username": "bob"})

    history = config.load_history()
    assert [e["username"] for e in history] == ["bob", "alice"]


def test_save_history_failure_preserves_existing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = [{"username": "alice"}]
    (tmp_path / "history.json").write_text(json.dumps(original))

    assert config.save_history([object()]) is False
    assert json.loads((tmp_path / "history.json").read_text()) == original


def test_save_history_failure_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "history.json").write_text("[]")

    config.save_history([object()])
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "history.json"]
    assert leftovers == []
