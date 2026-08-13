import os
import time
import types
from contextlib import closing

import pytest

import streamonitor.media_index as media_index
from streamonitor.managers.outofspace_detector import OOSDetector


@pytest.fixture(autouse=True)
def _no_min_video_size(monkeypatch):
    # Keep the tiny fixture files out of scan_once's undersized cleanup.
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


def _classify(abs_path, label):
    with closing(media_index.connect()) as conn:
        conn.execute("UPDATE media_files SET classification=? WHERE abs_path=?",
                     (label, os.path.abspath(abs_path)))
        conn.commit()


def _detector(monkeypatch, threshold=8.0, free_per_delete=2.0, free_start=5.0,
              streamers=None):
    # free_space rises by free_per_delete percent for each file deleted since
    # construction, simulating disk space being reclaimed.
    monkeypatch.setattr(media_index.parameters, "OOS_CLEANUP_THRESHOLD_PERCENT",
                        threshold)
    detector = OOSDetector(streamers or [])
    deleted = {"count": 0}
    real_remove = os.remove

    def fake_remove(path):
        real_remove(path)
        deleted["count"] += 1

    monkeypatch.setattr("streamonitor.managers.outofspace_detector.os.remove",
                        fake_remove)
    monkeypatch.setattr(OOSDetector, "free_space",
                        staticmethod(lambda: free_start + free_per_delete * deleted["count"]))
    return detector


def test_deletes_oldest_non_adult_first_and_stops_at_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    oldest = _make_video(tmp_path, filename="a.mp4", mtime=1_700_000_000)
    middle = _make_video(tmp_path, filename="b.mp4", mtime=1_700_000_500)
    newest = _make_video(tmp_path, filename="c.mp4", mtime=1_700_001_000)
    media_index.scan_once()
    for path in (oldest, middle, newest):
        _classify(path, "non-adult")
    # 5% free, +2%/deletion, threshold 8%: delete oldest two, keep the newest.
    _detector(monkeypatch).cleanup_non_adult()
    assert not os.path.exists(oldest)
    assert not os.path.exists(middle)
    assert os.path.exists(newest)
    # Deletions are synced to the index via mark_deleted.
    rows = media_index.list_videos("FS", "alice")
    assert [row["filename"] for row in rows] == ["c.mp4"]


def test_never_deletes_adult_or_unknown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    adult = _make_video(tmp_path, filename="adult.mp4")
    unknown = _make_video(tmp_path, filename="unknown.mp4")
    media_index.scan_once()
    _classify(adult, "adult")  # unknown.mp4 keeps the 'unknown' default
    _detector(monkeypatch, free_per_delete=0.0).cleanup_non_adult()
    assert os.path.exists(adult)
    assert os.path.exists(unknown)


def test_never_deletes_recording_streamer_files(tmp_path, monkeypatch):
    # Same username-prefix protection as the undersized cleanup: on-disk names
    # of an active recording can differ from recording_filename.
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path, filename="alice-20260812-234947.mp4")
    media_index.scan_once()
    _classify(path, "non-adult")
    streamer = types.SimpleNamespace(
        recording=True, username="alice",
        recording_filename="alice-20260706-120000.mp4",
        outputFolder=str(tmp_path / "downloads/alice [FS]"))
    _detector(monkeypatch, streamers=[streamer]).cleanup_non_adult()
    assert os.path.exists(path)


def test_never_deletes_recently_modified_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fresh = _make_video(tmp_path, filename="fresh.mp4", mtime=int(time.time()))
    media_index.scan_once()
    _classify(fresh, "non-adult")
    _detector(monkeypatch).cleanup_non_adult()
    assert os.path.exists(fresh)


def test_noop_when_free_space_above_threshold(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    _detector(monkeypatch, free_start=9.0).cleanup_non_adult()
    assert os.path.exists(path)


def test_noop_when_index_disabled_or_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    monkeypatch.setattr(media_index.parameters, "MEDIA_INDEX_ENABLED", False)
    _detector(monkeypatch).cleanup_non_adult()
    assert os.path.exists(path)
    # Missing db: candidate query must not create one.
    monkeypatch.setattr(media_index.parameters, "MEDIA_INDEX_ENABLED", True)
    os.remove(media_index.index_db_path())
    _detector(monkeypatch).cleanup_non_adult()
    assert os.path.exists(path)
    assert not os.path.exists(media_index.index_db_path())


def test_falls_back_to_exit_when_cleanup_insufficient(tmp_path, monkeypatch):
    # All candidates deleted but space is still low: disk_space_good stays
    # False so run() proceeds to the original CleanExit behavior.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("streamonitor.managers.outofspace_detector.MIN_FREE_DISK_PERCENT", 5.0)
    path = _make_video(tmp_path)
    media_index.scan_once()
    _classify(path, "non-adult")
    detector = _detector(monkeypatch, free_per_delete=0.5, free_start=3.0)
    detector.cleanup_non_adult()
    assert not os.path.exists(path)
    assert not detector.disk_space_good()
