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
