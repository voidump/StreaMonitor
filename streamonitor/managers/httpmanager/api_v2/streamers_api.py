"""Streamer collection endpoints: list, detail, schedule, add, lifecycle
actions, delete and batch operations."""
import os
import threading
from contextlib import closing
from time import sleep

from flask import request

import parameters
import streamonitor.media_index as media_index
import streamonitor.online_history as online_history

from .common import _json, _recording_disk_entry, _streamer_summary
from .media_cache import (_get_duration_cache_only, _maybe_cleanup_durations,
                          _thumb_cache_path, enqueue_duration_probe,
                          enqueue_thumb_prewarm)

# The list endpoint serves aggregates straight from the index, so externally
# deleted files (video_filter, temp repair, manual rm) would keep inflating
# videoCount/totalSize until the next scan or a detail request. Re-validation
# stats every indexed file, so it runs on a background thread at a fixed
# interval instead of on the request path: the list endpoint is polled
# frequently, and a per-file stat on every poll would undo the point of the
# aggregate query and tie the response to disk latency.
_LIST_VALIDATE_INTERVAL = 30  # seconds
_validate_manager = None
_validate_thread = None
_validate_thread_lock = threading.Lock()


def _validate_indexed_files(streamers):
    # Drop index rows whose file no longer exists (same isfile + mark_deleted
    # sync as the detail endpoint). Returns True when anything was marked
    # deleted.
    changed = False
    for s in streamers:
        rows = media_index.list_videos(s.siteslug, s.username)
        for row in rows or ():
            if not os.path.isfile(row['abs_path']):
                media_index.mark_deleted(row['abs_path'])
                changed = True
    return changed


def _validate_worker():
    # One daemon thread per process; it validates whichever manager registered
    # last (a running service has exactly one).
    while True:
        sleep(_LIST_VALIDATE_INTERVAL)
        manager = _validate_manager
        if manager is None:
            continue
        try:
            _validate_indexed_files(manager.streamers)
        except Exception:
            pass


def _ensure_validate_worker(manager):
    global _validate_manager, _validate_thread
    with _validate_thread_lock:
        _validate_manager = manager
        if _validate_thread is None or not _validate_thread.is_alive():
            _validate_thread = threading.Thread(
                target=_validate_worker, name='index-validate', daemon=True)
            _validate_thread.start()


def _recycle_root():
    # Recycle-bin root used for the videos' inRecycle flag. Prefer the
    # parameters getter once the recycle-bin engine lands it; until then follow
    # the agreed convention: '_recycle' resolved against the download dir.
    getter = (getattr(parameters, 'get_nsfw_recycle_dir', None)
              or getattr(parameters, 'get_recycle_dir', None))
    if callable(getter):
        try:
            return os.path.abspath(getter())
        except Exception:
            pass
    d = (getattr(parameters, 'NSFW_RECYCLE_DIR', None)
         or getattr(parameters, 'RECYCLE_DIR', None) or '_recycle')
    if not os.path.isabs(d):
        d = os.path.join(parameters.get_download_dir(), d)
    return os.path.abspath(d)


def _classification_map(siteslug, username):
    # {filename: classification} for one streamer in a single SQLite query (no
    # per-file disk IO). Only used while media_index.list_videos rows do not
    # carry a classification key themselves; any miss returns an empty dict so
    # the API reports null (not yet classified / index unavailable).
    try:
        if not media_index.is_readable():
            return {}
        with closing(media_index.connect()) as conn:
            rows = conn.execute(
                'SELECT filename, classification FROM media_files '
                'WHERE deleted=0 AND site=? AND username=?',
                (siteslug, username)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def register(app, manager, login_required):
    # Index re-validation happens off the request path (see _validate_worker).
    _ensure_validate_worker(manager)

    @app.route('/api/v2/streamers', methods=['GET'])
    @login_required
    def api_v2_streamers():
        # One aggregate SQLite query covers every streamer; entries missing from
        # the index (or a missing/disabled index) fall back per-streamer to the
        # cached video_files enumeration inside _streamer_summary.
        stats = media_index.get_stats_by_streamer() or {}
        payload = []
        for s in manager.streamers:
            s_stats = stats.get((s.siteslug, s.username))
            if s_stats is not None:
                # The index normally lacks the file being recorded; merge it
                # from disk so videoCount/totalSize/lastRecordedTs match the
                # cache-based path (which does include it). Skip the merge if
                # the scanner raced the recording start and already indexed it
                # (same dedup as the detail endpoint), or it would be counted
                # twice. Streamers with no indexed rows keep the
                # cached-enumeration fallback.
                rec = _recording_disk_entry(s)
                if rec is not None:
                    rec_name, _path, rec_size, rec_mtime = rec
                    if media_index.get_video(s.siteslug, s.username, rec_name) is None:
                        s_stats = {
                            'count': s_stats['count'] + 1,
                            'total_size': s_stats['total_size'] + rec_size,
                            'last_mtime': max(s_stats['last_mtime'] or 0, rec_mtime),
                        }
            payload.append(_streamer_summary(s, s_stats))
        return _json(payload)

    @app.route('/api/v2/streamers/<site>/<user>', methods=['GET'])
    @login_required
    def api_v2_streamer_detail(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        recording_name = getattr(streamer, 'recording_filename', None)
        # Prefer the media index (one SQLite query, durations included) over
        # re-scanning the directory. On any miss fall back to the existing
        # disk scan so behavior is unchanged when the index is off or cold.
        indexed = media_index.list_videos(streamer.siteslug, streamer.username)
        if indexed is not None:
            # Classification comes from the index only (never a per-file probe):
            # use a classification key on the rows when present, otherwise fetch
            # the whole streamer's labels in one extra query.
            cls_map = None
            if not (indexed and 'classification' in indexed[0]):
                cls_map = _classification_map(streamer.siteslug, streamer.username)
            # Externally deleted files (video_filter, temp repair, purge, manual
            # rm) are not reflected until the next scan; drop stale rows here
            # and sync the index so playback never 404s on a listed video.
            entries = []
            for row in indexed:
                if os.path.isfile(row['abs_path']):
                    cls = (row['classification'] if cls_map is None
                           else cls_map.get(row['filename']))
                    entries.append((row['filename'], row['abs_path'], row['size'],
                                    row['mtime'], row['duration'], cls))
                else:
                    media_index.mark_deleted(row['abs_path'])
            # The scanner skips the file the recorder is writing, so add it
            # from disk to keep the detail response identical to the scan path.
            if recording_name not in {e[0] for e in entries}:
                rec = _recording_disk_entry(streamer)
                if rec is not None:
                    entries.append(rec + (None, None))
        else:
            streamer.cache_file_list()
            entries = [(v.filename, v.abs_path, v.filesize,
                        int(v._stat.st_mtime), None, None) for v in streamer.video_files]
        recycle_prefix = _recycle_root() + os.sep
        videos = []
        for filename, abs_path, size, mtime, indexed_duration, classification in entries:
            is_recording = bool(streamer.recording and filename == recording_name)
            # Never block the response on ffprobe: use the index's duration or
            # the warm cache only, and queue uncached files for the background
            # worker (null until probed). The file the recorder is still
            # writing is skipped entirely.
            if is_recording:
                duration = None
            else:
                duration = indexed_duration
                if duration is None:
                    duration = _get_duration_cache_only(abs_path, mtime)
                if duration is None:
                    enqueue_duration_probe(abs_path, mtime)
                # Warm the thumbnail in the background (concurrency-capped) so it
                # is ready before the browser requests it. No-op once cached.
                enqueue_thumb_prewarm(
                    abs_path, _thumb_cache_path(abs_path, mtime), mtime)
            videos.append({
                'filename': filename,
                'size': size,
                'mtime': mtime,
                'duration': duration,
                'url': f'/video/{streamer.username}/{streamer.siteslug}/{filename}',
                'recording': is_recording,
                'classification': classification,
                'inRecycle': os.path.abspath(abs_path).startswith(recycle_prefix),
            })
        videos.sort(key=lambda v: v['mtime'], reverse=True)
        # Opportunistically drop durations.json entries for files that no longer
        # exist. Gated by size/growth so it stays off the common request path.
        _maybe_cleanup_durations(manager.streamers)
        # Keep the summary counters consistent with the videos we just listed
        # when they came from the index (the fallback path just re-reads the
        # freshly refreshed video_files cache).
        index_stats = None
        if indexed is not None:
            index_stats = {
                'count': len(videos),
                'total_size': sum(v['size'] for v in videos),
                'last_mtime': max((v['mtime'] for v in videos), default=None),
            }
        detail = _streamer_summary(streamer, index_stats)
        detail['videos'] = videos
        return _json(detail)

    @app.route('/api/v2/streamers/<site>/<user>/schedule', methods=['GET'])
    @login_required
    def api_v2_streamer_schedule(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # Backfill historical online hours from existing recordings (once per
        # streamer per process): file mtime is the session end, duration comes from
        # the warm durations cache (uncached files still contribute their end hour).
        try:
            streamer.cache_file_list()
            sessions = [
                (video._stat.st_mtime,
                 _get_duration_cache_only(video.abs_path, int(video._stat.st_mtime)))
                for video in streamer.video_files
            ]
            online_history.backfill(streamer.site, streamer.username, sessions)
        except Exception:
            pass
        return _json(online_history.compute_schedule(streamer.site, streamer.username))

    @app.route('/api/v2/streamers', methods=['POST'])
    @login_required
    def api_v2_add_streamer():
        data = request.get_json(silent=True) or {}
        username = data.get('username', '')
        site = data.get('site', '')
        if not username or not site:
            return _json({'error': 'Missing username or site'}, 400)
        existing = manager.getStreamer(username, site)
        res = manager.do_add(existing, username, site)
        if res.startswith('Added'):
            streamer = manager.getStreamer(username, site)
            return _json(_streamer_summary(streamer), 201)
        if res == 'Streamer already exists':
            return _json({'error': res}, 409)
        return _json({'error': res}, 400)

    _SINGLE_ACTIONS = {
        'pause': 'do_pause',
        'resume': 'do_resume',
        'stop-recording': 'do_stop_recording',
        'reactivate': 'do_reactivate',
    }

    @app.route('/api/v2/streamers/<site>/<user>/<action>', methods=['POST'])
    @login_required
    def api_v2_streamer_action(site, user, action):
        method = _SINGLE_ACTIONS.get(action)
        if method is None:
            return _json({'error': 'Unknown action'}, 404)
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        res = getattr(manager, method)(streamer, user, site)
        if res != 'OK':
            return _json({'error': res}, 500)
        return _json(_streamer_summary(streamer))

    @app.route('/api/v2/streamers/<site>/<user>', methods=['DELETE'])
    @login_required
    def api_v2_delete_streamer(site, user):
        data = request.get_json(silent=True) or {}
        delete_files = bool(data.get('deleteFiles', False))
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        res = manager.do_purge(streamer, user, site, delete_files=delete_files)
        if res != 'OK':
            return _json({'error': res}, 500)
        return _json({'ok': True})

    _BATCH_ACTIONS = {
        'pause': 'do_pause',
        'resume': 'do_resume',
        'stopRecording': 'do_stop_recording',
        'purge': 'do_purge',
    }

    @app.route('/api/v2/batch', methods=['POST'])
    @login_required
    def api_v2_batch():
        data = request.get_json(silent=True) or {}
        action = data.get('action')
        method = _BATCH_ACTIONS.get(action)
        if method is None:
            return _json({'error': 'Unknown action'}, 400)
        delete_files = bool(data.get('deleteFiles', False))
        results = []
        for target in data.get('targets', []):
            user = target.get('user')
            site = target.get('site')
            streamer = manager.getStreamer(user, site)
            if streamer is None:
                results.append({'site': site, 'user': user, 'ok': False, 'message': 'Streamer not found'})
                continue
            if action == 'purge':
                res = manager.do_purge(streamer, user, site, delete_files=delete_files)
            else:
                res = getattr(manager, method)(streamer, user, site)
            results.append({'site': site, 'user': user, 'ok': res == 'OK', 'message': res})
        return _json({'results': results})
