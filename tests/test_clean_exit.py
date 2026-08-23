import streamonitor.clean_exit as clean_exit_mod
from streamonitor.bot import Bot
from streamonitor.clean_exit import CleanExit


class FakeStreamer:
    def __init__(self, raise_on_stop=False, alive_forever=False):
        self.username = 'fake'
        self.stop_calls = []
        self.raise_on_stop = raise_on_stop
        self._alive = alive_forever

    def stop(self, a, b, thread_too=False):
        self.stop_calls.append(thread_too)
        if self.raise_on_stop:
            raise RuntimeError('boom')

    def is_alive(self):
        return self._alive


def _bare_clean_exit(streamers, timeout=None):
    # Skip __init__: it starts the singleton dummy thread and installs signal
    # handlers, which tests must not do.
    ce = CleanExit.__new__(CleanExit)
    ce.streamers = streamers
    if timeout is not None:
        ce.shutdown_timeout = timeout
    return ce


def test_one_failing_stop_does_not_abort_the_rest():
    # A single streamer whose stop() raises must not prevent the remaining
    # streamers from being told to stop (previously the loop just died).
    bad = FakeStreamer(raise_on_stop=True)
    good = FakeStreamer()
    _bare_clean_exit([bad, good]).clean_exit()
    assert bad.stop_calls == [True]
    assert good.stop_calls == [True]


def test_forced_exit_when_threads_never_stop(monkeypatch):
    exits = []
    monkeypatch.setattr(clean_exit_mod.os, '_exit', lambda code: exits.append(code))
    stuck = FakeStreamer(alive_forever=True)
    _bare_clean_exit([stuck], timeout=0.3).clean_exit()
    assert exits == [1]


def test_no_forced_exit_when_threads_stop(monkeypatch):
    exits = []
    monkeypatch.setattr(clean_exit_mod.os, '_exit', lambda code: exits.append(code))
    _bare_clean_exit([FakeStreamer()], timeout=0.3).clean_exit()
    assert exits == []


class _StopProbeBot(Bot):
    site = 'StopProbe'
    siteslug = 'SPB'


def test_bot_stop_survives_failing_download_callback():
    bot = _StopProbeBot('someone')
    bot.running = True

    def bad_callback():
        raise RuntimeError('download hook blew up')

    bot.stopDownload = bad_callback
    bot.stop(None, None, thread_too=True)  # must not raise
    assert bot.running is False
    assert bot.quitting is True


def test_bot_stop_clears_running_before_invoking_callback():
    # The callback must observe running=False: downloader loops key off it, and
    # setting it afterwards used to leave the bot "running" when the callback
    # raised from a signal handler.
    bot = _StopProbeBot('someone-else')
    bot.running = True
    seen = []
    bot.stopDownload = lambda: seen.append(bot.running)
    bot.stop(None, None)
    assert seen == [False]
