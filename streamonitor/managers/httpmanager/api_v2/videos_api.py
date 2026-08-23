"""Per-video endpoints: delete one/all, manual re-classification and
thumbnails."""
import os

from flask import request

import streamonitor.media_index as media_index

from .common import _json, _send_image, _THUMB_CACHE_CONTROL
from .media_cache import _generate_thumb_limited, _thumb_cache_path


def register(app, manager, login_required):
    @app.route('/api/v2/streamers/<site>/<user>/videos/<path:filename>', methods=['DELETE'])
    @login_required
    def api_v2_delete_video(site, user, filename):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # Second line of defense behind cache_file_list's exclusion: a .tmp.
        # name is a downloader's in-flight file, never deletable via the API.
        if '.tmp.' in filename:
            return _json({'error': 'Cannot delete an in-flight temp file'}, 403)
        # Double-safety with the frontend graying-out: never delete the file
        # that is currently being written by the recorder.
        if streamer.recording and filename == getattr(streamer, 'recording_filename', None):
            return _json({'error': 'Cannot delete a recording in progress'}, 409)
        streamer.cache_file_list()
        match = next((v for v in streamer.video_files if v.filename == filename), None)
        if match is None:
            return _json({'error': 'Video not found'}, 404)
        try:
            os.remove(match.abs_path)
        except Exception:
            return _json({'error': 'Failed to delete video'}, 500)
        media_index.mark_deleted(match.abs_path)
        streamer.cache_file_list()
        return _json({'ok': True})

    @app.route('/api/v2/streamers/<site>/<user>/videos', methods=['DELETE'])
    @login_required
    def api_v2_delete_all_videos(site, user):
        # Delete every recorded video file for this streamer, keeping the entry
        # and its monitor state intact. The file currently being written by the
        # recorder is skipped (never interrupt an in-progress recording).
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        streamer.cache_file_list()
        recording_name = getattr(streamer, 'recording_filename', None)
        deleted = 0
        skipped = 0
        freed = 0
        for video in list(streamer.video_files):
            # cache_file_list already excludes '.tmp.' names; keep the guard
            # here too so a stale list can never delete an in-flight file.
            if '.tmp.' in video.filename:
                skipped += 1
                continue
            if streamer.recording and video.filename == recording_name:
                skipped += 1
                continue
            try:
                size = video.filesize
                os.remove(video.abs_path)
                media_index.mark_deleted(video.abs_path)
                deleted += 1
                freed += size
            except Exception:
                skipped += 1
        streamer.cache_file_list()
        return _json({'ok': True, 'deleted': deleted, 'skipped': skipped, 'freed': freed})

    @app.route('/api/v2/videos/classification', methods=['POST'])
    @login_required
    def api_v2_set_video_classification():
        # Manual re-labeling. The file is addressed either by its absolute
        # path or by site/user/filename (what the video list carries); either
        # way it must resolve to an indexed row, so an unknown or crafted path
        # is a 404 and can never touch a file outside the index. Writes
        # classification_source='manual', which no automatic path overwrites,
        # and restores recycled files whose new label is not 'non-adult'.
        data = request.get_json(silent=True) or {}
        label = data.get('label')
        if label not in media_index.CLASSIFICATION_LABELS:
            return _json({'error': 'Invalid label', 'code': 'label'}, 400)
        path = data.get('path')
        if not path:
            site = data.get('site')
            user = data.get('user')
            filename = data.get('filename')
            if not (site and user and filename):
                return _json({'error': 'Missing path', 'code': 'path'}, 400)
            row = media_index.get_video(site, user, filename)
            if row is None:
                return _json({'error': 'Video not found'}, 404)
            path = row['abs_path']
        if not isinstance(path, str):
            return _json({'error': 'Missing path', 'code': 'path'}, 400)
        result = media_index.reclassify_manual(path, label)
        if 'error' in result:
            return _json({'error': result['error']}, result.get('status', 400))
        return _json({'ok': True, 'restored': result['restored'],
                      'classification': label})

    @app.route('/api/v2/streamers/<site>/<user>/videos/<path:filename>/thumb', methods=['GET'])
    @login_required
    def api_v2_video_thumb(site, user, filename):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # No thumbnail for the file the recorder is still writing.
        if streamer.recording and filename == getattr(streamer, 'recording_filename', None):
            return _json({'error': 'Recording in progress'}, 404)
        # Single-row index lookup avoids re-scanning the whole folder per thumb
        # request; a stale row (file gone) or any miss falls back to the scan.
        abs_path = mtime = None
        indexed = media_index.get_video(streamer.siteslug, streamer.username, filename)
        if indexed is not None and os.path.isfile(indexed['abs_path']):
            abs_path = indexed['abs_path']
            mtime = int(indexed['mtime'])
        else:
            streamer.cache_file_list()
            match = next((v for v in streamer.video_files if v.filename == filename), None)
            if match is None:
                return _json({'error': 'Video not found'}, 404)
            abs_path = match.abs_path
            mtime = int(match._stat.st_mtime)
        thumb_path = _thumb_cache_path(abs_path, mtime)
        # Concurrency-capped generation (shared with background prewarm); the
        # cached path returns immediately without touching the semaphore.
        if not _generate_thumb_limited(abs_path, thumb_path, mtime):
            # Broken/unparseable file: 404 so the frontend falls back to its
            # generated placeholder image.
            return _json({'error': 'Thumbnail unavailable'}, 404)
        return _send_image(thumb_path, 'image/jpeg', _THUMB_CACHE_CONTROL)
