from unittest import mock

import streamonitor.manager as manager_mod
from streamonitor.bot import Bot
from streamonitor.manager import Manager


class FakeStreamer:
    def __init__(self, username='alice', site='Chaturbate'):
        self.username = username
        self.site = site
        self.siteslug = 'CB'
        self.url = 'https://example.invalid/alice'
        self.logger = mock.Mock(handlers=[])
        self.outputFolder = '/nonexistent/alice'
        self.stop_calls = []

    def stop(self, a, b, thread_too=False):
        self.stop_calls.append(thread_too)

    def export(self):
        return {'site': self.site, 'username': self.username}


def _quiet_persistence(monkeypatch):
    monkeypatch.setattr(manager_mod.config, 'save_config', lambda data: True)
    monkeypatch.setattr(manager_mod.config, 'add_history_entry', lambda entry: True)


def test_do_remove_terminates_bot_thread(monkeypatch):
    # A removed bot leaves the streamers list, so CleanExit can never reach it
    # again: stop() must be told to end the thread too, otherwise the run()
    # loop spins forever.
    _quiet_persistence(monkeypatch)
    s = FakeStreamer()
    m = Manager([s])
    assert m.do_remove(s, s.username, s.site) == 'OK'
    assert s.stop_calls == [True]
    assert s not in m.streamers


def test_do_purge_terminates_bot_thread(monkeypatch):
    _quiet_persistence(monkeypatch)
    s = FakeStreamer()
    m = Manager([s])
    assert m.do_purge(s, s.username, s.site) == 'OK'
    assert s.stop_calls == [True]
    assert s not in m.streamers


def test_bot_threads_are_daemon():
    # Bots must not block interpreter shutdown: the orderly stop is CleanExit's
    # explicit wait, and a leaked/stuck bot thread must never hang the process.
    class _DaemonProbeBot(Bot):
        site = 'DaemonProbe'
        siteslug = 'DPB'

    bot = _DaemonProbeBot('someone')
    assert bot.daemon is True
