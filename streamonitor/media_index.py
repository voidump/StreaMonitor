import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from threading import Thread

import parameters
import streamonitor.log as log
from streamonitor.video_filter import remove_undersized_videos


logger = log.Logger("media_index").get_logger()

# Same extension whitelist as Bot.cache_file_list so the index and the per-bot
# file cache always agree on what counts as a video.
VIDEO_EXTENSIONS = ('mp4', 'mkv', 'webm', 'mov', 'avi', 'wmv')

# Streamer folders are named "<username> [<siteslug>]" (see Bot.outputFolder).
_FOLDER_RE = re.compile(r'^(?P<username>.+) \[(?P<site>[^\[\]]+)\]$')

# Never clean up files modified within this many seconds. Actively written
# files (ffmpeg strftime segments, hls _%03d parts, VR remux outputs, external
# writes) keep a fresh mtime while data is flowing, so this guard protects any
# write path even when the file is not matched to a recording streamer. 300s
# matches TempRepairScanner's active-file protection window.
CLEANUP_MTIME_GRACE_SECONDS = 300

# Serializes all writers (scanner thread + per-bot finished-recording calls);
# connections are short-lived so the db file can be deleted at any time and the
# next scan simply recreates it.
_db_lock = threading.Lock()

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    username TEXT NOT NULL,
    filename TEXT NOT NULL,
    abs_path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL DEFAULT 0,
    mtime INTEGER NOT NULL DEFAULT 0,
    duration REAL,
    thumb_path TEXT,
    classification TEXT NOT NULL DEFAULT 'unknown',
    deleted INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_files_site_user ON media_files (site, username);
CREATE INDEX IF NOT EXISTS idx_media_files_deleted ON media_files (deleted);
'''

# classification is deliberately absent from the update clause so a re-scan
# never clobbers a label assigned by the (future) NSFW analysis pipeline.
_UPSERT = '''
INSERT INTO media_files (site, username, filename, abs_path, size, mtime,
                         duration, thumb_path, deleted, updated_ts)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
ON CONFLICT(abs_path) DO UPDATE SET
    site=excluded.site,
    username=excluded.username,
    filename=excluded.filename,
    size=excluded.size,
    mtime=excluded.mtime,
    duration=excluded.duration,
    thumb_path=excluded.thumb_path,
    deleted=0,
    updated_ts=excluded.updated_ts
'''


def index_db_path():
    # Lives in .thumbs next to the duration/thumbnail caches, so it is never
    # scanned as a recording and moves along with the download directory.
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs', 'media_index.db'))


def connect():
    path = index_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(_SCHEMA)
    return conn


def _duration_and_thumb(abs_path, mtime):
    # Reuse api_v2's warm caches (both keyed by path+mtime) without ever
    # probing: duration comes from durations.json, and the thumb path is
    # recorded only once the cached frame actually exists on disk.
    try:
        from streamonitor.managers.httpmanager import api_v2
        duration = api_v2._get_duration_cache_only(abs_path, mtime)
        thumb_path = api_v2._thumb_cache_path(abs_path, mtime)
        return duration, thumb_path if os.path.exists(thumb_path) else None
    except Exception:
        return None, None


def _recording_paths(streamers):
    # Absolute paths of files currently being written by a recorder; these are
    # excluded from indexing and from deletion marking.
    skip = set()
    for streamer in streamers or []:
        try:
            if getattr(streamer, 'recording', False):
                name = getattr(streamer, 'recording_filename', None)
                if name:
                    skip.add(os.path.abspath(os.path.join(streamer.outputFolder, name)))
        except Exception:
            continue
    return skip


def _list_disk_videos(downloads_dir, skip_paths):
    videos = []
    try:
        folders = list(os.scandir(downloads_dir))
    except OSError as e:
        logger.warning(f'Cannot list download dir: {e}')
        return videos
    for folder in folders:
        try:
            if not folder.is_dir():
                continue
        except OSError:
            continue
        match = _FOLDER_RE.match(folder.name)
        if not match:
            continue
        site = match.group('site')
        username = match.group('username')
        try:
            entries = list(os.scandir(folder.path))
        except OSError:
            continue
        for file in entries:
            name = file.name
            # ".tmp." skips in-progress temp outputs (e.g. fmp4s' .tmp.mp4,
            # which would otherwise pass the extension whitelist).
            if name.startswith('.') or '.tmp.' in name:
                continue
            # Case-sensitive on purpose: Bot.cache_file_list does not lower the
            # extension, and the two views must agree or a file listed via the
            # index could never be deleted via the cache-based endpoint.
            if os.path.splitext(name)[1][1:] not in VIDEO_EXTENSIONS:
                continue
            try:
                if file.is_dir():
                    continue
                stat = file.stat()
            except OSError:
                continue
            abs_path = os.path.abspath(file.path)
            if abs_path in skip_paths:
                continue
            videos.append((site, username, name, abs_path, stat.st_size, int(stat.st_mtime)))
    return videos


def _cleanup_undersized(disk, streamers):
    # Library-level cleanup: delete undersized finished videos and drop them
    # from the disk list so they never (re)enter the index and the vanished-file
    # logic marks previously indexed ones deleted. A threshold of 0 disables
    # this entirely. Protections (any one is enough to keep a file):
    #   - .tmp. files and exact recording paths never reach `disk`
    #   - mtime within CLEANUP_MTIME_GRACE_SECONDS (actively written files)
    #   - any username-prefixed file in a recording streamer's outputFolder,
    #     because the real on-disk names differ from recording_filename under
    #     ffmpeg strftime segments, VR filename_extra_suffix and hls _%03d
    #     segmenting - all of them start with the username prefix
    min_mb = parameters.get_min_video_size_mb()
    if not min_mb or min_mb <= 0:
        return disk
    min_bytes = int(min_mb * 1024 * 1024)
    now = time.time()
    active = []  # (abs outputFolder, username) of streamers recording right now
    for streamer in streamers or []:
        try:
            if getattr(streamer, 'recording', False):
                active.append((os.path.abspath(streamer.outputFolder), streamer.username))
        except Exception:
            continue
    small = []
    for site, username, name, abs_path, size, mtime in disk:
        if size >= min_bytes:
            continue
        if now - mtime < CLEANUP_MTIME_GRACE_SECONDS:
            continue
        folder = os.path.dirname(abs_path)
        if any(folder == rec_folder and name.startswith(rec_user)
               for rec_folder, rec_user in active):
            continue
        small.append(abs_path)
    if small:
        removed = set(remove_undersized_videos(small, logger))
        if removed:
            disk = [entry for entry in disk if entry[3] not in removed]
    return disk


def scan_once(streamers=None):
    # One incremental sync pass: undersized-video cleanup first, then (index
    # enabled only) upsert new/changed files and mark vanished files deleted.
    # Cleanup deliberately does not depend on MEDIA_INDEX_ENABLED so the
    # threshold setting keeps working when the index is disabled; flags are
    # re-checked here (not just at startup) so runtime changes take effect.
    min_mb = parameters.get_min_video_size_mb()
    cleanup_enabled = bool(min_mb and min_mb > 0)
    if not parameters.MEDIA_INDEX_ENABLED and not cleanup_enabled:
        return
    downloads_dir = parameters.get_download_dir()
    if not os.path.isdir(downloads_dir):
        return
    skip_paths = _recording_paths(streamers)
    disk = _list_disk_videos(downloads_dir, skip_paths)
    disk = _cleanup_undersized(disk, streamers)
    if not parameters.MEDIA_INDEX_ENABLED:
        return
    now = int(time.time())
    with _db_lock, closing(connect()) as conn:
        existing = {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in conn.execute('SELECT abs_path, size, mtime, deleted, duration FROM media_files')
        }
        upserts = []
        for site, username, filename, abs_path, size, mtime in disk:
            prev = existing.get(abs_path)
            if prev and prev[0] == size and prev[1] == mtime and prev[2] == 0 and prev[3] is not None:
                continue
            duration, thumb_path = _duration_and_thumb(abs_path, mtime)
            upserts.append((site, username, filename, abs_path, size, mtime, duration, thumb_path, now))
        if upserts:
            conn.executemany(_UPSERT, upserts)
        seen = {entry[3] for entry in disk}
        gone = [
            (now, abs_path)
            for abs_path, prev in existing.items()
            if prev[2] == 0 and abs_path not in seen and abs_path not in skip_paths
        ]
        if gone:
            conn.executemany('UPDATE media_files SET deleted=1, updated_ts=? WHERE abs_path=?', gone)
        conn.commit()
    if upserts or gone:
        logger.info(f'Media index scan: {len(upserts)} upserted, {len(gone)} marked deleted')


def record_finished_file(streamer, filename):
    # Called from the bot's recording-ended path so a fresh file shows up in
    # the index immediately instead of waiting for the next scan. Never raises.
    try:
        if not parameters.MEDIA_INDEX_ENABLED or not filename:
            return
        abs_path = os.path.abspath(os.path.join(streamer.outputFolder, filename))
        if not os.path.isfile(abs_path):
            return  # e.g. removed by the undersized-video filter
        stat = os.stat(abs_path)
        mtime = int(stat.st_mtime)
        duration, thumb_path = _duration_and_thumb(abs_path, mtime)
        with _db_lock, closing(connect()) as conn:
            conn.execute(_UPSERT, (streamer.siteslug, streamer.username, filename,
                                   abs_path, stat.st_size, mtime, duration, thumb_path,
                                   int(time.time())))
            conn.commit()
    except Exception as e:
        logger.warning(f'Failed to index finished recording {filename}: {e}')


def record_repaired_file(abs_path):
    # Called by the periodic temp repair after producing a final file so it
    # shows up in the index immediately instead of waiting for the next scan.
    # The streamer is derived from the parent folder name. Never raises.
    try:
        if not parameters.MEDIA_INDEX_ENABLED or not abs_path:
            return
        abs_path = os.path.abspath(abs_path)
        if not os.path.isfile(abs_path):
            return  # e.g. removed by the undersized-video filter
        match = _FOLDER_RE.match(os.path.basename(os.path.dirname(abs_path)))
        if not match:
            return
        stat = os.stat(abs_path)
        mtime = int(stat.st_mtime)
        duration, thumb_path = _duration_and_thumb(abs_path, mtime)
        with _db_lock, closing(connect()) as conn:
            conn.execute(_UPSERT, (match.group('site'), match.group('username'),
                                   os.path.basename(abs_path), abs_path, stat.st_size,
                                   mtime, duration, thumb_path, int(time.time())))
            conn.commit()
    except Exception as e:
        logger.warning(f'Failed to index repaired file {abs_path}: {e}')


def _index_readable():
    # Read paths must never create the db file: a missing index means the
    # scanner has not produced anything yet and callers should fall back to
    # their existing disk-scan logic.
    return parameters.MEDIA_INDEX_ENABLED and os.path.isfile(index_db_path())


def list_videos(siteslug, username):
    # All indexed (non-deleted) videos for one streamer as a list of dicts with
    # filename, abs_path, size, mtime, duration and thumb_path. Returns None
    # when the index cannot serve the query (disabled, missing db, query error
    # or no rows) so the caller falls back to scanning the disk.
    if not _index_readable():
        return None
    try:
        with closing(connect()) as conn:
            rows = conn.execute(
                'SELECT filename, abs_path, size, mtime, duration, thumb_path '
                'FROM media_files WHERE deleted=0 AND site=? AND username=?',
                (siteslug, username)).fetchall()
    except Exception as e:
        logger.warning(f'Media index query failed for {username} [{siteslug}]: {e}')
        return None
    if not rows:
        return None
    return [
        {'filename': row[0], 'abs_path': row[1], 'size': row[2],
         'mtime': row[3], 'duration': row[4], 'thumb_path': row[5]}
        for row in rows
    ]


def get_video(siteslug, username, filename):
    # Single-file lookup for the thumbnail endpoint. Returns the same dict
    # shape as one list_videos entry, or None (miss -> caller scans the disk).
    if not _index_readable():
        return None
    try:
        with closing(connect()) as conn:
            row = conn.execute(
                'SELECT filename, abs_path, size, mtime, duration, thumb_path '
                'FROM media_files WHERE deleted=0 AND site=? AND username=? AND filename=?',
                (siteslug, username, filename)).fetchone()
    except Exception as e:
        logger.warning(f'Media index lookup failed for {filename}: {e}')
        return None
    if row is None:
        return None
    return {'filename': row[0], 'abs_path': row[1], 'size': row[2],
            'mtime': row[3], 'duration': row[4], 'thumb_path': row[5]}


def get_stats_by_streamer():
    # One aggregate query for the list endpoint: {(site, username): {count,
    # total_size, last_mtime}} across all indexed streamers, so summarising N
    # streamers costs one db round-trip instead of N directory scans. Returns
    # None when the index cannot serve the query (caller falls back to the
    # per-bot cached enumeration).
    if not _index_readable():
        return None
    try:
        with closing(connect()) as conn:
            rows = conn.execute(
                'SELECT site, username, COUNT(*), COALESCE(SUM(size), 0), MAX(mtime) '
                'FROM media_files WHERE deleted=0 GROUP BY site, username').fetchall()
    except Exception as e:
        logger.warning(f'Media index stats query failed: {e}')
        return None
    return {
        (row[0], row[1]): {'count': row[2], 'total_size': row[3], 'last_mtime': row[4]}
        for row in rows
    }


def mark_deleted(abs_path):
    # Called from the web API's delete endpoints so a manual deletion is
    # reflected immediately instead of waiting for the next scan. Never raises.
    try:
        if not _index_readable():
            return
        with _db_lock, closing(connect()) as conn:
            conn.execute('UPDATE media_files SET deleted=1, updated_ts=? WHERE abs_path=?',
                         (int(time.time()), os.path.abspath(abs_path)))
            conn.commit()
    except Exception as e:
        logger.warning(f'Failed to mark {abs_path} deleted in media index: {e}')


class MediaIndexScanner(Thread):
    # Background scanner: full pass at startup, then one incremental pass every
    # MEDIA_INDEX_SCAN_INTERVAL seconds. Any failure only logs and the loop
    # keeps running, so a broken scan never affects the main program.
    def __init__(self, streamers):
        super().__init__()
        self.daemon = True
        self.streamers = streamers

    def run(self):
        while True:
            try:
                scan_once(self.streamers)
            except Exception as e:
                logger.exception(f'Media index scan failed: {e}')
            # Re-read every cycle so a runtime settings change takes effect;
            # a 30s floor guards against a misconfigured busy loop.
            try:
                interval = int(parameters.MEDIA_INDEX_SCAN_INTERVAL)
            except Exception:
                interval = 300
            time.sleep(max(interval, 30))
