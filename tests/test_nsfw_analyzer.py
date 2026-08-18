import types

import pytest

import parameters
import streamonitor.nsfw_analyzer as nsfw_analyzer


class _NeverSetEvent:
    # Stand-in for RecordingMonitor._stop: never set, and wait() returns
    # immediately so the sampling loop can be driven without real sleeps.
    def __init__(self, max_waits=10):
        self.waits = 0
        self.max_waits = max_waits

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits > self.max_waits:
            raise AssertionError('sampling loop did not stop')
        return False

    def set(self):
        pass


def _monitor(tmp_path):
    bot = types.SimpleNamespace(
        username='alice',
        outputFolder=str(tmp_path),
        recording_filename='alice-20260706-120000.mp4',
    )
    return nsfw_analyzer.RecordingMonitor(bot)


@pytest.fixture(autouse=True)
def _nsfw_on(monkeypatch):
    monkeypatch.setattr(parameters, 'NSFW_ENABLED', True)


def test_run_stops_after_adult_frame(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path)
    monitor._stop = _NeverSetEvent()
    calls = []

    def _sample_once():
        calls.append(1)
        monitor.adult_seen = True

    monitor._sample_once = _sample_once
    monitor._run()

    assert len(calls) == 1  # no further ffmpeg frame grabs once adult


def test_run_keeps_sampling_while_not_adult(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path)
    monitor._stop = _NeverSetEvent(max_waits=3)
    calls = []

    monitor._sample_once = lambda: calls.append(1)
    with pytest.raises(AssertionError):
        monitor._run()

    assert len(calls) == 3
