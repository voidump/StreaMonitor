import os

import streamonitor.media_index as media_index


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
