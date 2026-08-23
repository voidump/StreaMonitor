import subprocess
import threading
import time
from unittest import mock

import streamonitor.downloaders.ffmpeg as ffmpeg_mod


class FakeProcess:
    def __init__(self, react_to_quit=True, react_to_terminate=True):
        self.returncode = None
        self.calls = []
        self._react_to_quit = react_to_quit
        self._react_to_terminate = react_to_terminate

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('ffmpeg', timeout)
        return self.returncode

    def communicate(self, input=None, timeout=None):
        self.calls.append('communicate')
        if not self._react_to_quit:
            raise subprocess.TimeoutExpired('ffmpeg', timeout)
        self.returncode = 255
        return b'', b''

    def terminate(self):
        self.calls.append('terminate')
        if self._react_to_terminate:
            self.returncode = -15

    def kill(self):
        self.calls.append('kill')
        self.returncode = -9


class FakeBot:
    headers = {'User-Agent': 'test-agent'}
    cookies = None

    def __init__(self):
        self.logger = mock.Mock()
        self.stopDownload = None

    def update_recording_progress(self, total):
        pass


def _run_with_stop(monkeypatch, tmp_path, process):
    monkeypatch.setattr(ffmpeg_mod.subprocess, 'Popen',
                        lambda *a, **kw: process)
    # Keep the escalation path fast in tests.
    monkeypatch.setattr(ffmpeg_mod, 'FFMPEG_QUIT_TIMEOUT', 0.05)
    monkeypatch.setattr(ffmpeg_mod, 'FFMPEG_TERM_TIMEOUT', 0.05)
    monkeypatch.setattr(ffmpeg_mod, 'FFMPEG_KILL_TIMEOUT', 0.05)

    bot = FakeBot()
    filename = str(tmp_path / 'user-20260823-120000.mp4')
    result = {}

    def run():
        result['ret'] = ffmpeg_mod.getVideoFfmpeg(bot, 'http://example.invalid/x.m3u8', filename)

    t = threading.Thread(target=run)
    t.start()
    # stopDownload must be armed before we can stop; it is assigned before the
    # worker thread starts, so this must become non-None almost immediately.
    deadline = time.time() + 5
    while bot.stopDownload is None and time.time() < deadline:
        time.sleep(0.01)
    assert bot.stopDownload is not None
    bot.stopDownload()
    t.join(timeout=10)
    assert not t.is_alive(), 'getVideoFfmpeg did not return after stop'
    return process, result['ret']


def test_stop_normal_quit(monkeypatch, tmp_path):
    process, _ = _run_with_stop(monkeypatch, tmp_path, FakeProcess())
    assert 'communicate' in process.calls
    assert 'terminate' not in process.calls
    assert 'kill' not in process.calls


def test_stop_escalates_to_terminate(monkeypatch, tmp_path):
    process, _ = _run_with_stop(
        monkeypatch, tmp_path, FakeProcess(react_to_quit=False))
    assert 'terminate' in process.calls
    assert 'kill' not in process.calls


def test_stop_escalates_to_kill(monkeypatch, tmp_path):
    process, _ = _run_with_stop(
        monkeypatch, tmp_path,
        FakeProcess(react_to_quit=False, react_to_terminate=False))
    assert process.calls[-1] == 'kill'
