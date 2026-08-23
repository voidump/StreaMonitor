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


def test_concurrent_remove_and_save_never_drops_unrelated_streamers(monkeypatch):
    # saveConfig exports by iterating the shared streamers list while other
    # threads remove entries; without the manager lock the shifting list makes
    # the export skip an unrelated streamer, which then gets persisted away.
    import threading

    saves = []
    monkeypatch.setattr(manager_mod.config, 'save_config',
                        lambda data: saves.append(data) or True)
    streamers = [FakeStreamer(username=f'u{i}') for i in range(50)]
    m = Manager(list(streamers))
    survivors = {f'u{i}' for i in range(25, 50)}

    def remove_first_half():
        for s in streamers[:25]:
            m.do_remove(s, s.username, s.site)

    def save_repeatedly():
        for _ in range(300):
            m.saveConfig()

    threads = [threading.Thread(target=remove_first_half),
               threading.Thread(target=save_repeatedly)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {s.username for s in m.streamers} == survivors
    for data in saves:
        exported = {entry['username'] for entry in data}
        # A save may or may not include not-yet-removed entries, but the 25
        # streamers nobody touched must be present in every single export.
        assert survivors <= exported, f'export lost unrelated streamers: {survivors - exported}'
