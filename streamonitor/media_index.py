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

# classification is NULL until the NSFW pipeline writes a label: NULL means
# "not analyzed" (the UI shows no badge), while 'unknown' means "analyzed but
# undecidable" (see set_classification / nsfw_analyzer).
_TABLE_SQL = '''
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
    classification TEXT,
    classification_source TEXT,
    deleted INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL DEFAULT 0
)'''

_INDEX_SQL = '''
CREATE INDEX IF NOT EXISTS idx_media_files_site_user ON media_files (site, username);
CREATE INDEX IF NOT EXISTS idx_media_files_deleted ON media_files (deleted);
'''

_SCHEMA = _TABLE_SQL + ';' + _INDEX_SQL

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


def _legacy_classification_default(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='media_files'"
    ).fetchone()
    return bool(row and row[0] and "DEFAULT 'unknown'" in row[0])


def _migrate_classification_default(conn):
    # Databases created before the nullable classification column labeled every
    # scanned file 'unknown' by default, so never-analyzed videos were
    # indistinguishable from real "analyzed but undecidable" results and the UI
    # badged everything as unknown. Rebuild the table without the default and
    # NULL out the legacy 'unknown' rows (overwhelmingly the scanner default;
    # labels the NSFW pipeline writes from now on are preserved as-is).
    if not _legacy_classification_default(conn):
        return
    try:
        conn.execute('BEGIN IMMEDIATE')
        if not _legacy_classification_default(conn):  # lost the migration race
            conn.execute('ROLLBACK')
            return
        conn.execute('ALTER TABLE media_files RENAME TO media_files_legacy')
        conn.execute(_TABLE_SQL)
        conn.execute('''INSERT INTO media_files (id, site, username, filename,
                            abs_path, size, mtime, duration, thumb_path,
                            classification, classification_source, deleted, updated_ts)
                        SELECT id, site, username, filename, abs_path, size,
                            mtime, duration, thumb_path,
                            NULLIF(classification, 'unknown'),
                            CASE WHEN classification = 'unknown' THEN NULL
                                 ELSE classification_source END,
                            deleted, updated_ts
                        FROM media_files_legacy''')
        conn.execute('DROP TABLE media_files_legacy')
        conn.commit()
        conn.executescript(_INDEX_SQL)
        logger.info('Media index migrated: classification default is now NULL')
    except Exception as e:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        logger.warning(f'Media index classification migration failed: {e}')


def _migrate_classification_source(conn):
    # Databases created before manual re-labeling have no classification_source
    # column. Add it and mark every existing label 'auto' (they can only have
    # come from the analysis pipeline); rows without a label stay NULL.
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(media_files)')}
        if 'classification_source' in columns:
            return
        conn.execute('ALTER TABLE media_files ADD COLUMN classification_source TEXT')
        conn.execute("UPDATE media_files SET classification_source='auto' "
                     "WHERE classification IS NOT NULL")
        conn.commit()
        logger.info('Media index migrated: classification_source column added')
    except Exception as e:
        logger.warning(f'Media index classification_source migration failed: {e}')


def connect():
    path = index_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(_SCHEMA)
    # Order matters: the column must exist before the legacy-default rebuild
    # copies it into the new table.
    _migrate_classification_source(conn)
    _migrate_classification_default(conn)
    return conn


def _duration_cache_entry(abs_path, mtime):
    # (found, duration) from api_v2's warm cache; (False, None) when the web
    # API is unavailable. Lets the scanner tell "never probed / probe failed"
    # from "probed successfully".
    try:
        from streamonitor.managers.httpmanager.api_v2 import media_cache
        return media_cache.get_duration_cache_entry(abs_path, mtime)
    except Exception:
        return False, None


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
    roots = [os.path.abspath(downloads_dir)]
    # Relocated non-adult videos keep the "<username> [<siteslug>]" folder
    # layout under the non-adult directory; scan it too (when present) so
    # moved files stay indexed instead of being marked deleted. Scanned
    # regardless of the relocation toggle so turning it off later never
    # hides files that were already moved. The folder name "_nonadult" does
    # not match _FOLDER_RE, so a nested default location is never scanned
    # twice via the downloads pass. The NSFW recycle bin follows the same
    # convention so recycled videos stay listed and playable until the
    # retention purge removes them.
    for extra in (parameters.get_nonadult_dir(), parameters.get_nsfw_recycle_dir()):
        extra = os.path.abspath(extra)
        if extra not in roots and os.path.isdir(extra):
            roots.append(extra)
    for root in roots:
        try:
            folders = list(os.scandir(root))
        except OSError as e:
            logger.warning(f'Cannot list {root}: {e}')
            continue
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
            if prev and prev[0] == size and prev[1] == mtime and prev[2] == 0:
                if prev[3] is not None:
                    continue
                # Row exists but its duration is NULL: only rewrite once the
                # warm cache actually knows a duration. A cache miss (never
                # probed) or a cached probe failure used to re-upsert every
                # such row on every scan cycle — endless churn for broken or
                # never-viewed files.
                found, duration = _duration_cache_entry(abs_path, mtime)
                if not found or duration is None:
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
    _nsfw_maintenance(streamers)


def _rekey_video_caches(old_abs, new_abs, mtime):
    # Migrate api_v2's duration and thumbnail cache entries (keyed by
    # abs_path+mtime) to the new path so a moved file keeps its cached
    # duration and thumbnail instead of re-probing. Best effort; never raises.
    try:
        from streamonitor.managers.httpmanager import api_v2
        return api_v2.rekey_video_caches(old_abs, new_abs, mtime)
    except Exception:
        return None


def relocate_non_adult(streamers=None):
    # Move finished videos classified 'non-adult' (label written by the NSFW
    # pipeline; 'adult'/'unknown'/empty are never touched) into the non-adult
    # directory, preserving the "<username> [<siteslug>]" folder layout so the
    # index, the /video route fallback and Bot.cache_file_list all keep
    # serving them. Same safety standard as the undersized cleanup: exact
    # recording paths, any username-prefixed file in a recording streamer's
    # folder, and files modified within CLEANUP_MTIME_GRACE_SECONDS are never
    # moved. os.rename only (atomic, same filesystem); a cross-device target
    # logs and leaves the file in place. Returns the number of files moved.
    if not parameters.NONADULT_RELOCATE_ENABLED or not _index_readable():
        return 0
    root = os.path.abspath(parameters.get_nonadult_dir())
    if root == os.path.abspath(parameters.get_download_dir()):
        return 0
    try:
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT abs_path, filename, mtime FROM media_files "
                "WHERE deleted=0 AND classification='non-adult'").fetchall()
    except Exception as e:
        logger.warning(f'Non-adult relocation query failed: {e}')
        return 0
    # Files already in the recycle bin are waiting for the retention purge;
    # relocating them out would silently extend their lifetime.
    recycle_root = os.path.abspath(parameters.get_nsfw_recycle_dir())
    excluded = (root + os.sep, recycle_root + os.sep)
    candidates = [row for row in rows
                  if not os.path.abspath(row[0]).startswith(excluded)]
    if not candidates:
        return 0
    now = time.time()
    recording_paths = _recording_paths(streamers)
    active = []  # (abs outputFolder, username) of streamers recording right now
    for streamer in streamers or []:
        try:
            if getattr(streamer, 'recording', False):
                active.append((os.path.abspath(streamer.outputFolder), streamer.username))
        except Exception:
            continue
    moved = 0
    for abs_path, filename, _db_mtime in candidates:
        abs_path = os.path.abspath(abs_path)
        if abs_path in recording_paths:
            continue
        try:
            stat = os.stat(abs_path)
        except OSError:
            continue  # vanished; the next scan marks it deleted
        if now - stat.st_mtime < CLEANUP_MTIME_GRACE_SECONDS:
            continue
        src_folder = os.path.dirname(abs_path)
        if any(src_folder == rec_folder and filename.startswith(rec_user)
               for rec_folder, rec_user in active):
            continue
        folder_name = os.path.basename(src_folder)
        if not _FOLDER_RE.match(folder_name):
            continue
        dest_dir = os.path.join(root, folder_name)
        dest = os.path.join(dest_dir, filename)
        if os.path.exists(dest):
            logger.warning(f'Non-adult relocation skipped, target exists: {dest}')
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            os.rename(abs_path, dest)
        except OSError as e:
            logger.warning(f'Non-adult relocation failed for {abs_path}: {e}')
            continue
        mtime = int(stat.st_mtime)
        new_thumb = _rekey_video_caches(abs_path, dest, mtime)
        try:
            with _db_lock, closing(connect()) as conn:
                conn.execute(
                    'UPDATE media_files SET abs_path=?, thumb_path=?, updated_ts=? '
                    'WHERE abs_path=?',
                    (dest, new_thumb, int(time.time()), abs_path))
                conn.commit()
        except Exception as e:
            # The rename already happened; the next scan self-heals by marking
            # the old row deleted and upserting the new path (classification
            # is lost in that case, which is safe: worst case is a re-label).
            logger.warning(f'Non-adult index update failed for {dest}: {e}')
        moved += 1
    if moved:
        logger.info(f'Non-adult relocation: moved {moved} video(s) to {root}')
    return moved


def _active_recorders(streamers):
    # (abs outputFolder, username) of streamers recording right now; any
    # username-prefixed file in such a folder is treated as possibly being
    # written (ffmpeg strftime segments, VR suffixes, hls _%03d parts).
    active = []
    for streamer in streamers or []:
        try:
            if getattr(streamer, 'recording', False):
                active.append((os.path.abspath(streamer.outputFolder), streamer.username))
        except Exception:
            continue
    return active


def apply_nsfw_action(streamers=None, only_paths=None):
    # Unified non-adult handling engine. Applies the runtime NSFW action
    # ('recycle' or 'delete'; 'mark_only' and unknown values are a no-op) to
    # indexed videos classified 'non-adult'. Adult/unknown/unclassified rows
    # are never selected, so they can never be moved or deleted here. The
    # action and all other settings are read at call time so runtime changes
    # take effect on the next trigger. `only_paths` narrows the pass to the
    # given files (instant trigger after classification); classification is
    # still re-read from the index. Safety guards match relocate_non_adult:
    # exact recording paths, username-prefixed files in a recording
    # streamer's folder, .tmp. names and files modified within
    # CLEANUP_MTIME_GRACE_SECONDS are never touched.
    #   recycle: move into the recycle bin preserving the "<username>
    #     [<siteslug>]" layout; mtime is reset to the move time so the daily
    #     purge measures retention from the recycle moment.
    #   delete: remove the file and mark the index row deleted.
    # Files already under the recycle bin (waiting for the retention purge)
    # or under the legacy non-adult archive (a previously curated library
    # that a policy switch must not wipe) are excluded. Returns the number
    # of files handled.
    action = parameters.get_nsfw_action()
    if action not in ('recycle', 'delete'):
        return 0
    if not parameters.get_nsfw_enabled() or not _index_readable():
        return 0
    downloads_root = os.path.abspath(parameters.get_download_dir())
    recycle_root = os.path.abspath(parameters.get_nsfw_recycle_dir())
    nonadult_root = os.path.abspath(parameters.get_nonadult_dir())
    if action == 'recycle' and recycle_root == downloads_root:
        return 0
    protected = [r + os.sep for r in (recycle_root, nonadult_root)
                 if r != downloads_root]
    try:
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT abs_path, filename FROM media_files "
                "WHERE deleted=0 AND classification='non-adult'").fetchall()
    except Exception as e:
        logger.warning(f'NSFW action query failed: {e}')
        return 0
    wanted = None
    if only_paths is not None:
        wanted = {os.path.abspath(p) for p in only_paths}
    candidates = []
    for abs_path, filename in rows:
        abs_path = os.path.abspath(abs_path)
        if wanted is not None and abs_path not in wanted:
            continue
        if any(abs_path.startswith(prefix) for prefix in protected):
            continue
        candidates.append((abs_path, filename))
    if not candidates:
        return 0
    now = time.time()
    recording_paths = _recording_paths(streamers)
    active = _active_recorders(streamers)
    handled = 0
    for abs_path, filename in candidates:
        if abs_path in recording_paths or '.tmp.' in filename:
            continue
        try:
            stat = os.stat(abs_path)
        except OSError:
            continue  # vanished; the next scan marks it deleted
        if now - stat.st_mtime < CLEANUP_MTIME_GRACE_SECONDS:
            continue
        src_folder = os.path.dirname(abs_path)
        if any(src_folder == rec_folder and filename.startswith(rec_user)
               for rec_folder, rec_user in active):
            continue
        if action == 'delete':
            try:
                os.remove(abs_path)
            except OSError as e:
                logger.warning(f'NSFW delete failed for {abs_path}: {e}')
                continue
            mark_deleted(abs_path)
            logger.info(f'NSFW action: deleted non-adult video {filename}')
            handled += 1
            continue
        folder_name = os.path.basename(src_folder)
        if not _FOLDER_RE.match(folder_name):
            continue
        dest_dir = os.path.join(recycle_root, folder_name)
        dest = os.path.join(dest_dir, filename)
        if os.path.exists(dest):
            logger.warning(f'NSFW recycle skipped, target exists: {dest}')
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            os.rename(abs_path, dest)
        except OSError as e:
            logger.warning(f'NSFW recycle failed for {abs_path}: {e}')
            continue
        # Reset mtime so the retention purge counts from the recycle moment
        # (an old recording recycled by a backfill scan must still get its
        # full retention window). The old path+mtime cache keys are stale
        # after this, so the thumb is cleared and regenerated lazily.
        recycled_ts = int(time.time())
        try:
            os.utime(dest, (recycled_ts, recycled_ts))
        except OSError:
            recycled_ts = int(stat.st_mtime)
        try:
            with _db_lock, closing(connect()) as conn:
                conn.execute(
                    'UPDATE media_files SET abs_path=?, mtime=?, thumb_path=NULL, '
                    'updated_ts=? WHERE abs_path=?',
                    (dest, recycled_ts, int(time.time()), abs_path))
                conn.commit()
        except Exception as e:
            # The rename already happened; the next scan self-heals by marking
            # the old row deleted and upserting the new path.
            logger.warning(f'NSFW recycle index update failed for {dest}: {e}')
        logger.info(f'NSFW action: recycled non-adult video {filename}')
        handled += 1
    if handled:
        logger.info(f'NSFW action ({action}): handled {handled} non-adult video(s)')
    return handled


def purge_recycle_bin():
    # Daily retention purge: permanently remove recycle-bin videos older than
    # NSFW_RECYCLE_RETENTION_DAYS (measured by mtime, which apply_nsfw_action
    # resets to the recycle moment). Only runs while the NSFW feature is
    # enabled so switching it off freezes the bin. Disk-based on purpose: it
    # keeps working across index rebuilds. Empty per-streamer folders are
    # removed afterwards. Returns the number of files removed.
    if not parameters.get_nsfw_enabled():
        return 0
    root = os.path.abspath(parameters.get_nsfw_recycle_dir())
    if root == os.path.abspath(parameters.get_download_dir()) or not os.path.isdir(root):
        return 0
    try:
        days = int(parameters.get_nsfw_recycle_retention_days())
    except Exception:
        days = 7
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        folders = [f for f in os.scandir(root) if f.is_dir()]
    except OSError as e:
        logger.warning(f'Cannot list recycle bin {root}: {e}')
        return 0
    for folder in folders:
        try:
            entries = list(os.scandir(folder.path))
        except OSError:
            continue
        for file in entries:
            name = file.name
            if name.startswith('.') or '.tmp.' in name:
                continue
            if os.path.splitext(name)[1][1:] not in VIDEO_EXTENSIONS:
                continue
            try:
                if file.is_dir():
                    continue
                stat = file.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            abs_path = os.path.abspath(file.path)
            try:
                os.remove(abs_path)
            except OSError as e:
                logger.warning(f'Recycle bin purge failed for {abs_path}: {e}')
                continue
            mark_deleted(abs_path)
            removed += 1
        try:
            os.rmdir(folder.path)  # only succeeds when empty
        except OSError:
            pass
    if removed:
        logger.info(f'Recycle bin purge: removed {removed} expired video(s)')
    return removed


# Scheduling state for the NSFW maintenance pass (module-level so the
# scanner-thread cadence survives across scan_once calls; both start at 0 so
# the first scan after startup runs immediately).
_nsfw_last_action_ts = 0.0
_nsfw_last_purge_ts = 0.0
_NSFW_PURGE_INTERVAL_SECONDS = 24 * 3600


def _nsfw_maintenance(streamers=None):
    # Called at the end of every scan_once pass. All settings are re-read
    # here so runtime changes apply on the next pass:
    #   - When the NSFW feature is on and the action is recycle/delete, the
    #     scheduled engine pass runs every NSFW_SCAN_INTERVAL_HOURS hours
    #     (0 disables it; the instant trigger still applies the action).
    #   - Otherwise the legacy non-adult relocation keeps its original
    #     every-scan behavior (still gated by NONADULT_RELOCATE_ENABLED).
    #   - The recycle-bin retention purge runs daily while the feature is on,
    #     regardless of the current action, so files recycled under an older
    #     policy never linger past their retention window.
    global _nsfw_last_action_ts, _nsfw_last_purge_ts
    try:
        nsfw_on = parameters.get_nsfw_enabled()
        action = parameters.get_nsfw_action()
        if not (nsfw_on and action in ('recycle', 'delete')):
            relocate_non_adult(streamers)
        else:
            try:
                hours = int(parameters.get_nsfw_scan_interval_hours())
            except Exception:
                hours = 0
            now = time.time()
            if hours > 0 and now - _nsfw_last_action_ts >= hours * 3600:
                _nsfw_last_action_ts = now
                apply_nsfw_action(streamers)
        if nsfw_on:
            now = time.time()
            if now - _nsfw_last_purge_ts >= _NSFW_PURGE_INTERVAL_SECONDS:
                _nsfw_last_purge_ts = now
                purge_recycle_bin()
    except Exception as e:
        logger.warning(f'NSFW maintenance failed: {e}')


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


def is_readable():
    """Public form of _index_readable for other modules (the web API)."""
    return _index_readable()


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


CLASSIFICATION_LABELS = ('adult', 'non-adult', 'unknown')


def set_classification(abs_path, classification):
    # Sole automatic writer of the classification column (NSFW analysis
    # pipeline: recording finalize and the backfill). Uses an upsert so
    # labeling works even when the row has not been inserted yet by the
    # scanner or the finished-recording hook; a later re-scan never clobbers
    # the label (classification is absent from _UPSERT's update clause). Rows
    # labeled by hand (classification_source='manual') are never overwritten,
    # so the guard covers every automatic path at once. Never raises.
    try:
        if classification not in CLASSIFICATION_LABELS:
            return
        if not parameters.MEDIA_INDEX_ENABLED:
            return
        abs_path = os.path.abspath(abs_path)
        if not os.path.isfile(abs_path):
            return  # e.g. removed by the undersized-video filter meanwhile
        match = _FOLDER_RE.match(os.path.basename(os.path.dirname(abs_path)))
        if not match:
            return
        stat = os.stat(abs_path)
        with _db_lock, closing(connect()) as conn:
            conn.execute('''INSERT INTO media_files (site, username, filename, abs_path,
                                                     size, mtime, classification,
                                                     classification_source, deleted, updated_ts)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', 0, ?)
                            ON CONFLICT(abs_path) DO UPDATE SET
                                classification=excluded.classification,
                                classification_source='auto',
                                updated_ts=excluded.updated_ts
                            WHERE COALESCE(media_files.classification_source, '') <> 'manual' ''',
                         (match.group('site'), match.group('username'),
                          os.path.basename(abs_path), abs_path, stat.st_size,
                          int(stat.st_mtime), classification, int(time.time())))
            conn.commit()
    except Exception as e:
        logger.warning(f'Failed to set classification for {abs_path}: {e}')


def _unique_destination(dest):
    # "name.mp4" -> "name (1).mp4" until the path is free, so restoring never
    # overwrites an unrelated file that took the original name meanwhile.
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    for i in range(1, 1000):
        candidate = f'{base} ({i}){ext}'
        if not os.path.exists(candidate):
            return candidate
    return None


def _restore_from_recycle(abs_path, filename):
    # Move a recycled file back to <downloads>/<username> [<siteslug>]/ (the
    # recycle bin mirrors that layout, so the original folder name is simply
    # the parent folder's name). Returns the new absolute path, or None when
    # nothing was restored. Best effort; never raises.
    folder_name = os.path.basename(os.path.dirname(abs_path))
    if not _FOLDER_RE.match(folder_name):
        return None
    dest_dir = os.path.join(os.path.abspath(parameters.get_download_dir()), folder_name)
    dest = _unique_destination(os.path.join(dest_dir, filename))
    if dest is None:
        logger.warning(f'Restore skipped, no free target name for {abs_path}')
        return None
    try:
        os.makedirs(dest_dir, exist_ok=True)
        os.rename(abs_path, dest)
    except OSError as e:
        logger.warning(f'Restore failed for {abs_path}: {e}')
        return None
    return dest


def reclassify_manual(abs_path, classification):
    # Manual re-labeling from the web UI. Writes the label with
    # classification_source='manual' (never overwritten by any automatic
    # path) and, when the file currently sits in the recycle bin and the new
    # label is not 'non-adult', restores it to its original download folder
    # with the index, mtime and thumbnail cache kept in sync.
    # Returns {'ok': True, 'restored': bool, 'path': str} or an
    # {'error': ..., 'status': int} dict.
    if classification not in CLASSIFICATION_LABELS:
        return {'error': 'Invalid label', 'status': 400}
    if not _index_readable():
        return {'error': 'Media index unavailable', 'status': 404}
    abs_path = os.path.abspath(abs_path)
    try:
        with closing(connect()) as conn:
            row = conn.execute(
                'SELECT filename FROM media_files WHERE deleted=0 AND abs_path=?',
                (abs_path,)).fetchone()
    except Exception as e:
        logger.warning(f'Manual reclassification lookup failed for {abs_path}: {e}')
        return {'error': 'Media index unavailable', 'status': 404}
    if row is None:
        return {'error': 'Video not found', 'status': 404}
    filename = row[0]
    recycle_root = os.path.abspath(parameters.get_nsfw_recycle_dir())
    in_recycle = (recycle_root != os.path.abspath(parameters.get_download_dir())
                  and abs_path.startswith(recycle_root + os.sep))
    new_path = abs_path
    restored = False
    if in_recycle and classification != 'non-adult' and os.path.isfile(abs_path):
        dest = _restore_from_recycle(abs_path, filename)
        if dest is not None:
            new_path = dest
            restored = True
    try:
        mtime = int(os.stat(new_path).st_mtime)
    except OSError:
        mtime = None
    thumb_path = None
    if restored:
        thumb_path = _rekey_video_caches(abs_path, new_path, mtime)
    try:
        with _db_lock, closing(connect()) as conn:
            if restored:
                conn.execute(
                    'UPDATE media_files SET abs_path=?, filename=?, mtime=?, '
                    'thumb_path=?, classification=?, classification_source=?, '
                    'updated_ts=? WHERE abs_path=?',
                    (new_path, os.path.basename(new_path), mtime, thumb_path,
                     classification, 'manual', int(time.time()), abs_path))
            else:
                conn.execute(
                    'UPDATE media_files SET classification=?, '
                    'classification_source=?, updated_ts=? WHERE abs_path=?',
                    (classification, 'manual', int(time.time()), abs_path))
            conn.commit()
    except Exception as e:
        logger.warning(f'Manual reclassification failed for {abs_path}: {e}')
        return {'error': 'Failed to update classification', 'status': 500}
    logger.info(f'Manual classification {classification} for {filename}'
                + (' (restored from recycle bin)' if restored else ''))
    return {'ok': True, 'restored': restored, 'path': new_path}


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
