from streamonitor.bot import Bot
from streamonitor.enums import Status


def make_bot(bulk_update, sc, last_ts=None):
    """Build a Bot without running its network/filesystem __init__."""
    b = Bot.__new__(Bot)
    b.bulk_update = bulk_update
    b.sc = sc
    b._last_unknown_fallback_ts = last_ts
    return b


def test_non_bulk_site_always_checks():
    # Non-bulk sites poll on their own cadence, never throttled here.
    b = make_bot(bulk_update=False, sc=Status.UNKNOWN)
    assert b._should_do_single_status_check(1000.0) is True


def test_bulk_notrunning_always_checks():
    # NOTRUNNING keeps its original behaviour: single fallback every loop.
    b = make_bot(bulk_update=True, sc=Status.NOTRUNNING)
    assert b._should_do_single_status_check(1000.0) is True


def test_bulk_unknown_first_time_checks():
    # No prior fallback recorded -> allow an immediate check.
    b = make_bot(bulk_update=True, sc=Status.UNKNOWN, last_ts=None)
    assert b._should_do_single_status_check(1000.0) is True


def test_bulk_unknown_within_backoff_skips():
    # Within the backoff window the single fallback must not fire.
    b = make_bot(bulk_update=True, sc=Status.UNKNOWN, last_ts=1000.0)
    within = 1000.0 + Bot.min_unknown_fallback_interval - 1
    assert b._should_do_single_status_check(within) is False


def test_bulk_unknown_after_backoff_checks():
    # Once the interval has elapsed the fallback fires again.
    b = make_bot(bulk_update=True, sc=Status.UNKNOWN, last_ts=1000.0)
    after = 1000.0 + Bot.min_unknown_fallback_interval
    assert b._should_do_single_status_check(after) is True


def test_bulk_settled_status_does_not_check():
    # A bulk streamer with a real status relies on the manager, no fallback.
    for status in (Status.PUBLIC, Status.OFFLINE, Status.PRIVATE, Status.LONG_OFFLINE):
        b = make_bot(bulk_update=True, sc=status)
        assert b._should_do_single_status_check(1000.0) is False
