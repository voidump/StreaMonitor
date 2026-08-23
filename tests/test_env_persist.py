import os
import stat
import threading

import pytest

import parameters


def test_legacy_persist_functions_write_through_shared_writer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parameters.persist_download_dir('/somewhere/downloads')
    parameters.persist_min_video_size_mb(5.0)
    parameters.persist_nsfw_enabled(True)
    parameters._persist_env_key('STRMNTR_EXTRA', 'kept')
    content = open('.env').read()
    assert 'STRMNTR_DOWNLOAD_DIR=/somewhere/downloads\n' in content
    assert 'STRMNTR_MIN_VIDEO_SIZE_MB=5\n' in content
    assert 'STRMNTR_NSFW_ENABLED=true\n' in content
    assert 'STRMNTR_EXTRA=kept\n' in content


def test_env_file_is_private(tmp_path, monkeypatch):
    # .env holds the web password in plain text; it must not be created
    # world-readable (and re-saving must not loosen a tightened file).
    monkeypatch.chdir(tmp_path)
    parameters._persist_env_key('STRMNTR_EXTRA', 'x')
    assert stat.S_IMODE(os.stat('.env').st_mode) == 0o600
    parameters.persist_download_dir('/d')
    assert stat.S_IMODE(os.stat('.env').st_mode) == 0o600


def test_update_existing_key_preserves_other_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with open('.env', 'w') as f:
        f.write('# comment\nSTRMNTR_HOST=127.0.0.1\nSTRMNTR_PORT=5000\n')
    parameters._persist_env_key('STRMNTR_PORT', '5001')
    lines = open('.env').read().splitlines()
    assert lines == ['# comment', 'STRMNTR_HOST=127.0.0.1', 'STRMNTR_PORT=5001']


def test_values_with_line_breaks_are_rejected(tmp_path, monkeypatch):
    # "abcd\nSTRMNTR_HOST=0.0.0.0" as a password would inject an arbitrary
    # config line into .env on the next parse.
    monkeypatch.chdir(tmp_path)
    for bad in ('a\nb', 'a\rb', 'a\0b'):
        with pytest.raises(ValueError):
            parameters._persist_env_key('STRMNTR_PASSWORD', bad)
    assert not os.path.exists('.env')


def test_concurrent_persists_do_not_lose_keys(tmp_path, monkeypatch):
    # Read-modify-write from many threads: with the old shared ".env.tmp" and
    # no lock, concurrent saves truncated each other and dropped keys.
    monkeypatch.chdir(tmp_path)
    errors = []

    def write(i):
        try:
            parameters._persist_env_key(f'STRMNTR_KEY_{i}', str(i))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    content = open('.env').read()
    for i in range(20):
        assert f'STRMNTR_KEY_{i}={i}\n' in content
