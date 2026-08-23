import os

from ffmpy import FFRuntimeError

import streamonitor.temp_repair as temp_repair


def _write_tmp(tmp_path, name='alice-20260101-000000.tmp.ts', size=1024):
    folder = tmp_path / 'downloads' / 'alice [CB]'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b'0' * size)
    return path


def _quiet_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(temp_repair.parameters, 'get_download_dir',
                        lambda: str(tmp_path / 'downloads'))
    monkeypatch.setattr(temp_repair, 'remove_undersized_videos',
                        lambda paths, logger: None)
    monkeypatch.setattr(temp_repair, 'record_repaired_file', lambda path: None)


class _FailingFFmpeg:
    exit_code = 1

    def __init__(self, executable=None, inputs=None, outputs=None):
        self.outputs = outputs

    def run(self, stdout=None, stderr=None):
        raise FFRuntimeError('ffmpeg', self.exit_code, b'', b'')


class _InterruptedFFmpeg(_FailingFFmpeg):
    exit_code = 255


class _SucceedingFFmpeg:
    def __init__(self, executable=None, inputs=None, outputs=None):
        self.outputs = outputs

    def run(self, stdout=None, stderr=None):
        output_path = list(self.outputs)[0]
        with open(output_path, 'wb') as f:
            f.write(b'x' * 2048)


def test_nonzero_exit_keeps_temp_file(tmp_path, monkeypatch):
    # ffmpeg exits 1 for a corrupt input AND for ENOSPC/permission errors on
    # the output — indistinguishable by exit code. Deleting the temp file here
    # used to destroy the only copy of a recording in the disk-full restart
    # loop (OOS exit -> restart -> repair -> ENOSPC -> delete).
    _quiet_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr(temp_repair, 'FFmpeg', _FailingFFmpeg)
    tmp_file = _write_tmp(tmp_path)
    temp_repair._scan_and_repair()
    assert tmp_file.exists(), 'temp file must survive a failed remux'
    # The partial remux output must not be left behind.
    leftovers = [p for p in tmp_file.parent.iterdir() if '.tmp.' not in p.name]
    assert leftovers == []


def test_interrupted_exit_keeps_temp_file(tmp_path, monkeypatch):
    _quiet_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr(temp_repair, 'FFmpeg', _InterruptedFFmpeg)
    tmp_file = _write_tmp(tmp_path)
    temp_repair._scan_and_repair()
    assert tmp_file.exists()


def test_successful_repair_removes_temp_file(tmp_path, monkeypatch):
    _quiet_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr(temp_repair, 'FFmpeg', _SucceedingFFmpeg)
    tmp_file = _write_tmp(tmp_path)
    temp_repair._scan_and_repair()
    assert not tmp_file.exists()
    produced = [p for p in tmp_file.parent.iterdir()]
    assert len(produced) == 1
    assert produced[0].suffix == '.' + temp_repair.CONTAINER


def test_empty_temp_file_is_removed(tmp_path, monkeypatch):
    _quiet_side_effects(monkeypatch, tmp_path)
    monkeypatch.setattr(temp_repair, 'FFmpeg', _SucceedingFFmpeg)
    tmp_file = _write_tmp(tmp_path, size=0)
    temp_repair._scan_and_repair()
    assert not tmp_file.exists()
    assert list(tmp_file.parent.iterdir()) == []
