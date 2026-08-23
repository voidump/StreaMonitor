import time

from streamonitor.bot import Bot
from streamonitor.enums import Status


class _CookieProbeBot(Bot):
    site = 'CookieProbe'
    siteslug = 'CKP'


def _make_bot():
    bot = _CookieProbeBot('someone')
    bot.cookie_update_interval = 1
    bot.cookieUpdater = lambda: True
    bot.sc = Status.PUBLIC
    bot.running = True
    bot.quitting = False
    return bot


def test_cookie_thread_not_duplicated():
    # Recordings end and restart while the streamer stays PUBLIC; each round
    # used to spawn a fresh updater thread while the old one never exited.
    bot = _make_bot()
    try:
        bot._ensure_cookie_thread()
        first = bot._cookie_thread
        assert first is not None and first.is_alive()
        bot._ensure_cookie_thread()
        bot._ensure_cookie_thread()
        assert bot._cookie_thread is first
    finally:
        bot.quitting = True
        bot._cookie_thread.join(timeout=5)
    assert not bot._cookie_thread.is_alive()


def test_cookie_thread_respawns_after_exit():
    bot = _make_bot()
    bot._ensure_cookie_thread()
    first = bot._cookie_thread
    bot.quitting = True
    first.join(timeout=5)
    assert not first.is_alive()

    bot.quitting = False
    try:
        bot._ensure_cookie_thread()
        assert bot._cookie_thread is not first
        assert bot._cookie_thread.is_alive()
    finally:
        bot.quitting = True
        bot._cookie_thread.join(timeout=5)


def test_cookie_thread_does_not_update_after_stop():
    # The loop re-checks its exit conditions after sleeping so a stop during
    # the sleep can't trigger one final network call.
    bot = _make_bot()
    calls = []
    bot.cookieUpdater = lambda: calls.append(1) or True
    bot._ensure_cookie_thread()
    bot.quitting = True
    bot._cookie_thread.join(timeout=5)
    assert calls == []
    time.sleep(0.1)
    assert calls == []
