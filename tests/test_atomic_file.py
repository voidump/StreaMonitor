import json
import os
import stat
import threading

import pytest

from streamonitor.utils.atomic_file import atomic_write_json, atomic_write_text


def test_write_creates_file_with_content(tmp_path):
    target = tmp_path / 'out.json'
    atomic_write_json(str(target), {'a': 1})
    assert json.loads(target.read_text()) == {'a': 1}


def test_write_replaces_existing_file(tmp_path):
    target = tmp_path / 'out.json'
    target.write_text('old')
    atomic_write_json(str(target), [1, 2, 3])
    assert json.loads(target.read_text()) == [1, 2, 3]


def test_failed_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / 'missing-dir' / 'out.json'
    with pytest.raises(OSError):
        atomic_write_text(str(target), 'x')
    assert list(tmp_path.iterdir()) == []


def test_mode_is_applied(tmp_path):
    target = tmp_path / 'secret.env'
    atomic_write_text(str(target), 'KEY=value\n', mode=0o600)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_default_mode_honors_umask(tmp_path):
    # mkstemp creates 0600 temp files; without an explicit chmod the rename
    # would silently tighten config.json & co. The default must reproduce
    # what a plain open() would have created under the current umask.
    umask = os.umask(0)
    os.umask(umask)
    target = tmp_path / 'config.json'
    atomic_write_json(str(target), {})
    assert stat.S_IMODE(os.stat(target).st_mode) == (0o666 & ~umask)


def test_concurrent_writers_never_corrupt_target(tmp_path):
    # 20 threads hammering the same path: the final file must be one writer's
    # complete, valid JSON. The old shared "<path>.tmp" scheme let writer B
    # truncate A's half-written temp and error on the shared rename.
    target = tmp_path / 'config.json'
    payloads = [{'writer': i, 'data': ['x' * 100] * 50} for i in range(20)]
    errors = []

    def write(payload):
        try:
            atomic_write_json(str(target), payload)
        except Exception as e:  # pragma: no cover - failure detail
            errors.append(e)

    threads = [threading.Thread(target=write, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    final = json.loads(target.read_text())
    assert final in payloads
    leftovers = [p for p in tmp_path.iterdir() if p.name != 'config.json']
    assert leftovers == []
