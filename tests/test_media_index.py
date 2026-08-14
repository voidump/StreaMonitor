import os
import time
import types

import pytest

import streamonitor.media_index as media_index


@pytest.fixture(autouse=True)
def _no_min_video_size(monkeypatch):
    # scan_once deletes finished videos under MIN_VIDEO_SIZE_MB (default 10MB);
    # disable it so the tiny fixture files survive the scan.
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 0)


@pytest.fixture(autouse=True)
def _reset_nsfw_schedule():
    # The scheduled NSFW action/purge cadence is module state; reset it so
    # every test starts with a due first pass.
    media_index._nsfw_last_action_ts = 0.0
    media_index._nsfw_last_purge_ts = 0.0


def _make_video(tmp_path, username="alice", site="FS",
                filename="alice-20260706-120000.mp4", data=b"0" * 1000,
                mtime=1_700_000_000):
    folder = tmp_path / f"downloads/{username} [{site}]"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return str(path)


def test_scan_once_upserts_and_list_videos(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path)
    media_index.scan_once()
    rows = media_index.list_videos("FS", "alice")
    assert rows is not None and len(rows) == 1
    row = rows[0]
    assert row["filename"] == "alice-20260706-120000.mp4"
    assert row["abs_path"] == os.path.abspath(path)
    assert row["size"] == 1000
    assert row["mtime"] == 1_700_000_000


def test_scan_once_marks_vanished_files_deleted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path)
    media_index.scan_once()
    os.remove(path)
    media_index.scan_once()
    assert media_index.list_videos("FS", "alice") is None


def test_scan_once_respects_runtime_disable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path)
    monkeypatch.setattr(media_index.parameters, "MEDIA_INDEX_ENABLED", False)
    media_index.scan_once()
    assert not os.path.exists(media_index.index_db_path())


def test_scan_extension_check_matches_bot_case_sensitivity(tmp_path, monkeypatch):
    # Bot.cache_file_list does not lower the extension; the index must agree so
    # an uppercase-extension file is never listed via the index but 404s on the
    # cache-based delete endpoint.
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path, filename="alice-20260706-120000.MP4")
    media_index.scan_once()
    assert media_index.list_videos("FS", "alice") is None


def test_get_video_single_lookup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path)
    media_index.scan_once()
    row = media_index.get_video("FS", "alice", "alice-20260706-120000.mp4")
    assert row is not None and row["size"] == 1000
    assert media_index.get_video("FS", "alice", "nope.mp4") is None


def test_get_stats_by_streamer_aggregates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path, filename="a.mp4", data=b"0" * 100, mtime=1_700_000_000)
    _make_video(tmp_path, filename="b.mp4", data=b"0" * 50, mtime=1_700_000_500)
    media_index.scan_once()
    stats = media_index.get_stats_by_streamer()
    assert stats[("FS", "alice")] == {
        "count": 2, "total_size": 150, "last_mtime": 1_700_000_500}


def test_mark_deleted_hides_row_immediately(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path)
    media_index.scan_once()
    media_index.mark_deleted(path)
    assert media_index.list_videos("FS", "alice") is None
    assert media_index.get_stats_by_streamer() == {}


def test_reads_return_none_without_db(tmp_path, monkeypatch):
    # Read paths must signal fallback and never create the db file.
    monkeypatch.chdir(tmp_path)
    assert media_index.list_videos("FS", "alice") is None
    assert media_index.get_video("FS", "alice", "x.mp4") is None
    assert media_index.get_stats_by_streamer() is None
    media_index.mark_deleted("/nonexistent/x.mp4")
    assert not os.path.exists(media_index.index_db_path())


def test_reads_return_none_when_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path)
    media_index.scan_once()
    monkeypatch.setattr(media_index.parameters, "MEDIA_INDEX_ENABLED", False)
    assert media_index.list_videos("FS", "alice") is None
    assert media_index.get_stats_by_streamer() is None


def _recording_streamer(tmp_path, username="alice", site="FS",
                        recording_filename="alice-20260706-120000.mp4"):
    folder = str(tmp_path / f"downloads/{username} [{site}]")
    return types.SimpleNamespace(recording=True, username=username,
                                 recording_filename=recording_filename,
                                 outputFolder=folder)


@pytest.mark.parametrize("filename", [
    # ffmpeg -f segment -strftime: on-disk name differs from recording_filename
    "alice-20260812-234947.mp4",
    # VR sites append filename_extra_suffix to the base name
    "alice-20260706-120000_LR_F180.mp4",
    # hls/fmp4s post-processing writes _%03d segments
    "alice-20260706-120000_001.mp4",
])
def test_cleanup_keeps_recording_streamer_files(tmp_path, monkeypatch, filename):
    # Regression for P1: files actively written by a recorder whose name does
    # not equal recording_filename must survive cleanup. mtime is old on
    # purpose so the username-prefix exclusion (not the grace window) is what
    # protects them.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 10)
    path = _make_video(tmp_path, filename=filename)
    media_index.scan_once([_recording_streamer(tmp_path)])
    assert os.path.exists(path)


def test_cleanup_mtime_grace_protects_unmatched_files(tmp_path, monkeypatch):
    # Regression for P1 fallback: a small file with a fresh mtime is never
    # cleaned up even when no recording streamer matches it (external writes,
    # recording just ended, etc.).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 10)
    now = time.time()
    fresh = _make_video(tmp_path, filename="fresh.mp4", mtime=int(now))
    old = _make_video(tmp_path, filename="old.mp4")
    media_index.scan_once()
    assert os.path.exists(fresh)
    assert not os.path.exists(old)


def test_cleanup_deletes_other_streamers_old_small_files(tmp_path, monkeypatch):
    # The recording exclusion is scoped to the recording streamer's folder and
    # username prefix; other streamers' old small files are still cleaned up.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 10)
    other = _make_video(tmp_path, username="bob", filename="bob-20260101-000000.mp4")
    media_index.scan_once([_recording_streamer(tmp_path)])
    assert not os.path.exists(other)


def test_cleanup_runs_when_index_disabled(tmp_path, monkeypatch):
    # Regression for P2: the undersized cleanup must not depend on
    # MEDIA_INDEX_ENABLED, but index writes stay off (no db created).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "MEDIA_INDEX_ENABLED", False)
    monkeypatch.setattr(media_index.parameters, "MIN_VIDEO_SIZE_MB", 10)
    small = _make_video(tmp_path, filename="small.mp4")
    big = _make_video(tmp_path, filename="big.mp4", data=b"0" * (11 * 1024 * 1024))
    media_index.scan_once()
    assert not os.path.exists(small)
    assert os.path.exists(big)
    assert not os.path.exists(media_index.index_db_path())


def _classify(abs_path, label):
    from contextlib import closing
    with closing(media_index.connect()) as conn:
        conn.execute("UPDATE media_files SET classification=? WHERE abs_path=?",
                     (label, os.path.abspath(abs_path)))
        conn.commit()


def test_scanned_but_unanalyzed_video_has_null_classification(tmp_path, monkeypatch):
    # The scanner never labels files; classification stays NULL ("not
    # analyzed") until the NSFW pipeline writes one, so the UI shows no badge.
    from contextlib import closing
    monkeypatch.chdir(tmp_path)
    _make_video(tmp_path)
    media_index.scan_once()
    with closing(media_index.connect()) as conn:
        row = conn.execute("SELECT classification FROM media_files").fetchone()
    assert row[0] is None


def test_legacy_unknown_default_migrates_to_null(tmp_path, monkeypatch):
    # Databases created with the old DEFAULT 'unknown' schema are rebuilt on
    # connect: scanner-default 'unknown' rows become NULL, real labels survive.
    import sqlite3
    from contextlib import closing
    monkeypatch.chdir(tmp_path)
    db_path = media_index.index_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    legacy_schema = media_index._TABLE_SQL.replace(
        "classification TEXT,", "classification TEXT NOT NULL DEFAULT 'unknown',")
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(legacy_schema)
        conn.execute("INSERT INTO media_files (site, username, filename, abs_path) "
                     "VALUES ('FS', 'alice', 'a.mp4', '/x/a.mp4')")
        conn.execute("INSERT INTO media_files (site, username, filename, abs_path, "
                     "classification) VALUES ('FS', 'alice', 'b.mp4', '/x/b.mp4', 'adult')")
        conn.commit()
    with closing(media_index.connect()) as conn:
        rows = dict(conn.execute(
            "SELECT filename, classification FROM media_files").fetchall())
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_files'"
        ).fetchone()[0]
    assert rows == {"a.mp4": None, "b.mp4": "adult"}
    assert "DEFAULT 'unknown'" not in table_sql


def test_relocate_moves_non_adult_and_updates_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index.scan_once()
    dest = tmp_path / "downloads/_nonadult/alice [FS]/alice-20260706-120000.mp4"
    assert not os.path.exists(path)
    assert dest.is_file()
    rows = media_index.list_videos("FS", "alice")
    assert rows is not None and len(rows) == 1
    assert rows[0]["abs_path"] == str(dest)
    # A subsequent scan keeps listing the moved file (the non-adult tree is
    # scanned too) and never marks it deleted or moves it again.
    media_index.scan_once()
    rows = media_index.list_videos("FS", "alice")
    assert rows is not None and rows[0]["abs_path"] == str(dest)


def test_relocate_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index.scan_once()
    assert os.path.exists(path)


def test_relocate_never_touches_adult_or_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    adult = _make_video(tmp_path, filename="a.mp4")
    unknown = _make_video(tmp_path, filename="b.mp4")
    media_index.scan_once()
    _classify(adult, "adult")
    media_index.scan_once()
    assert os.path.exists(adult)
    assert os.path.exists(unknown)


def test_relocate_skips_recently_modified_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    path = _make_video(tmp_path, mtime=int(time.time()))
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index.scan_once()
    assert os.path.exists(path)


@pytest.mark.parametrize("filename", [
    "alice-20260812-234947.mp4",
    "alice-20260706-120000_LR_F180.mp4",
    "alice-20260706-120000_001.mp4",
])
def test_relocate_skips_recording_streamer_files(tmp_path, monkeypatch, filename):
    # Same protection as the undersized cleanup: any username-prefixed file in
    # a recording streamer's folder is never moved even with an old mtime.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    path = _make_video(tmp_path, filename=filename)
    streamer = _recording_streamer(tmp_path)
    media_index.scan_once([streamer])
    _classify(path, "non-adult")
    media_index.scan_once([streamer])
    assert os.path.exists(path)


def test_relocate_skips_existing_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    path = _make_video(tmp_path)
    dest = tmp_path / "downloads/_nonadult/alice [FS]/alice-20260706-120000.mp4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"other")
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index.scan_once()
    assert os.path.exists(path)
    assert dest.read_bytes() == b"other"


def _enable_nsfw(monkeypatch, action):
    monkeypatch.setattr(media_index.parameters, "NSFW_ENABLED", True)
    monkeypatch.setattr(media_index.parameters, "NSFW_ACTION", action)


def test_apply_action_recycles_and_rekeys_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action() == 1
    dest = tmp_path / "downloads/_recycle/alice [FS]/alice-20260706-120000.mp4"
    assert not os.path.exists(path)
    assert dest.is_file()
    # mtime is reset to the recycle moment so retention counts from the move.
    assert time.time() - os.path.getmtime(dest) < 60
    rows = media_index.list_videos("FS", "alice")
    assert rows is not None and len(rows) == 1
    assert rows[0]["abs_path"] == str(dest)
    # A subsequent scan keeps listing the recycled file and never moves or
    # re-recycles it (the recycle root is excluded from candidates).
    media_index.scan_once()
    assert media_index.apply_nsfw_action() == 0
    rows = media_index.list_videos("FS", "alice")
    assert rows is not None and rows[0]["abs_path"] == str(dest)


def test_apply_action_deletes_and_marks_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "delete")
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action() == 1
    assert not os.path.exists(path)
    assert media_index.list_videos("FS", "alice") is None


def test_apply_action_mark_only_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "mark_only")
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action() == 0
    assert os.path.exists(path)


def test_apply_action_requires_nsfw_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NSFW_ENABLED", False)
    monkeypatch.setattr(media_index.parameters, "NSFW_ACTION", "delete")
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action() == 0
    assert os.path.exists(path)


def test_apply_action_never_touches_adult_or_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "delete")
    adult = _make_video(tmp_path, filename="a.mp4")
    unknown = _make_video(tmp_path, filename="b.mp4")
    media_index.scan_once()
    _classify(adult, "adult")
    # b.mp4 stays 'unknown' (the scanner default) and is passed explicitly:
    # only_paths never overrides the classification re-read from the index.
    assert media_index.apply_nsfw_action(only_paths=[adult, unknown]) == 0
    assert os.path.exists(adult)
    assert os.path.exists(unknown)


def test_apply_action_skips_fresh_mtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "delete")
    path = _make_video(tmp_path, mtime=int(time.time()))
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action(only_paths=[path]) == 0
    assert os.path.exists(path)


@pytest.mark.parametrize("filename", [
    "alice-20260812-234947.mp4",
    "alice-20260706-120000_LR_F180.mp4",
    "alice-20260706-120000_001.mp4",
])
def test_apply_action_skips_recording_streamer_files(tmp_path, monkeypatch, filename):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "delete")
    path = _make_video(tmp_path, filename=filename)
    streamer = _recording_streamer(tmp_path)
    media_index.scan_once([streamer])
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action([streamer]) == 0
    assert os.path.exists(path)


def test_apply_action_only_paths_narrows_pass(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "delete")
    first = _make_video(tmp_path, filename="a.mp4")
    second = _make_video(tmp_path, filename="b.mp4")
    media_index.scan_once()
    _classify(first, "non-adult")
    _classify(second, "non-adult")
    assert media_index.apply_nsfw_action(only_paths=[first]) == 1
    assert not os.path.exists(first)
    assert os.path.exists(second)


def test_apply_action_recycle_skips_existing_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    path = _make_video(tmp_path)
    dest = tmp_path / "downloads/_recycle/alice [FS]/alice-20260706-120000.mp4"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"other")
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action() == 0
    assert os.path.exists(path)
    assert dest.read_bytes() == b"other"


def test_apply_action_spares_legacy_nonadult_archive(tmp_path, monkeypatch):
    # Files previously moved into the non-adult archive are never deleted or
    # recycled by a later policy switch.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index.scan_once()
    dest = tmp_path / "downloads/_nonadult/alice [FS]/alice-20260706-120000.mp4"
    assert dest.is_file()
    _enable_nsfw(monkeypatch, "delete")
    assert media_index.apply_nsfw_action() == 0
    assert dest.is_file()


def test_scan_once_runs_scheduled_action(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index._nsfw_last_action_ts = 0.0  # interval elapsed since last pass
    media_index.scan_once()
    dest = tmp_path / "downloads/_recycle/alice [FS]/alice-20260706-120000.mp4"
    assert not os.path.exists(path)
    assert dest.is_file()


def test_scan_once_scheduled_action_respects_interval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    path = _make_video(tmp_path)
    media_index.scan_once()  # first pass consumes the due window
    _classify(path, "non-adult")
    media_index.scan_once()  # within the interval: not processed yet
    assert os.path.exists(path)


def test_scan_once_scheduled_action_zero_interval_disables(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    monkeypatch.setattr(media_index.parameters, "NSFW_SCAN_INTERVAL_HOURS", 0)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    media_index.scan_once()
    assert os.path.exists(path)


def test_purge_removes_expired_recycled_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    folder = tmp_path / "downloads/_recycle/alice [FS]"
    folder.mkdir(parents=True)
    expired = folder / "old.mp4"
    expired.write_bytes(b"0" * 100)
    old_ts = time.time() - 8 * 86400
    os.utime(expired, (old_ts, old_ts))
    fresh = folder / "new.mp4"
    fresh.write_bytes(b"0" * 100)
    assert media_index.purge_recycle_bin() == 1
    assert not expired.exists()
    assert fresh.exists()


def test_purge_removes_empty_folders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    folder = tmp_path / "downloads/_recycle/alice [FS]"
    folder.mkdir(parents=True)
    expired = folder / "old.mp4"
    expired.write_bytes(b"0" * 100)
    old_ts = time.time() - 8 * 86400
    os.utime(expired, (old_ts, old_ts))
    assert media_index.purge_recycle_bin() == 1
    assert not folder.exists()


def test_purge_disabled_when_nsfw_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NSFW_ENABLED", False)
    folder = tmp_path / "downloads/_recycle/alice [FS]"
    folder.mkdir(parents=True)
    expired = folder / "old.mp4"
    expired.write_bytes(b"0" * 100)
    old_ts = time.time() - 8 * 86400
    os.utime(expired, (old_ts, old_ts))
    assert media_index.purge_recycle_bin() == 0
    assert expired.exists()


def test_purge_respects_retention_days_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _enable_nsfw(monkeypatch, "recycle")
    monkeypatch.setattr(media_index.parameters, "NSFW_RECYCLE_RETENTION_DAYS", 30)
    folder = tmp_path / "downloads/_recycle/alice [FS]"
    folder.mkdir(parents=True)
    aged = folder / "old.mp4"
    aged.write_bytes(b"0" * 100)
    old_ts = time.time() - 8 * 86400  # older than 7 but younger than 30
    os.utime(aged, (old_ts, old_ts))
    assert media_index.purge_recycle_bin() == 0
    assert aged.exists()


def test_legacy_relocate_never_moves_recycled_files(tmp_path, monkeypatch):
    # Switching back to mark_only + legacy relocation must not pull files out
    # of the recycle bin (that would silently extend their lifetime).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(media_index.parameters, "NONADULT_RELOCATE_ENABLED", True)
    _enable_nsfw(monkeypatch, "recycle")
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    assert media_index.apply_nsfw_action() == 1
    recycled = tmp_path / "downloads/_recycle/alice [FS]/alice-20260706-120000.mp4"
    assert recycled.is_file()
    old_ts = time.time() - 3600  # age past the mtime grace window
    os.utime(recycled, (old_ts, old_ts))
    media_index.scan_once()
    monkeypatch.setattr(media_index.parameters, "NSFW_ACTION", "mark_only")
    media_index.scan_once()
    assert recycled.is_file()
