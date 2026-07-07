import hashlib
import json
import os
import re
import subprocess
from time import sleep, time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

import requests
from flask import Response, request, send_file

import parameters
import streamonitor.config as config
from parameters import WEB_LIST_FREQUENCY, FFMPEG_PATH
from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.enums import Status
from streamonitor.managers.outofspace_detector import OOSDetector
from streamonitor.utils import human_file_size


# Stable machine-readable keys derived from the backend Status enum. The web UI
# combines these with `running` and `monitorState` to derive paused/stopped.
_STATUS_KEYS = {
    Status.PUBLIC: 'online',
    Status.OFFLINE: 'offline',
    Status.LONG_OFFLINE: 'long_offline',
    Status.PRIVATE: 'private',
    Status.RATELIMIT: 'ratelimit',
    Status.ERROR: 'error',
    Status.RESTRICTED: 'restricted',
    Status.NOTEXIST: 'notexist',
    Status.NOTRUNNING: 'notrunning',
    Status.UNKNOWN: 'unknown',
}


def _status_key(sc):
    return _STATUS_KEYS.get(sc, 'unknown')


# Live view (watch-while-recording) tuning. We start the tail a bit before the
# write head so latency stays low. For raw MPEG-TS the start is aligned to a
# 188-byte packet boundary; for fragmented MP4 (what StripChat/CMAF actually
# writes into the .tmp.ts) we prepend the init segment and start at a recent
# fragment boundary. These are module-level so tests can shrink them.
_TS_PACKET_SIZE = 188
_LIVE_TAIL_BYTES = 1 << 20  # ~1 MB backoff from the write head
_LIVE_READ_CHUNK = 64 * 1024
_LIVE_POLL_INTERVAL = 0.5  # seconds between reads when caught up to the tail
_LIVE_MAX_IDLE_ITERS = 60  # give up after this many empty reads (~30s)


def _fmp4_init_end(f):
    # Parse ISO-BMFF top-level boxes from the start and return the offset just
    # past the 'moov' box (the init segment = ftyp + ... + moov). None if not
    # found within a sane number of boxes.
    f.seek(0)
    offset = 0
    for _ in range(64):
        header = f.read(8)
        if len(header) < 8:
            return None
        size = int.from_bytes(header[0:4], 'big')
        box_type = header[4:8]
        if size == 1:  # 64-bit largesize
            ext = f.read(8)
            if len(ext) < 8:
                return None
            size = int.from_bytes(ext, 'big')
        if size < 8:
            return None
        if box_type == b'moov':
            return offset + size
        offset += size
        f.seek(offset)
    return None


def _fmp4_last_moof(window, base):
    # Find the absolute offset of the last 'moof' box in `window` that is
    # immediately followed by an 'mdat' box (a self-contained CMAF fragment).
    i = 0
    last = None
    while True:
        j = window.find(b'moof', i)
        if j < 0:
            break
        if j >= 4:
            size = int.from_bytes(window[j - 4:j], 'big')
            nxt = (j - 4) + size
            if 8 <= size and nxt + 8 <= len(window) and window[nxt + 4:nxt + 8] == b'mdat':
                last = base + (j - 4)
        i = j + 4
    return last


def _live_tmp_path(streamer):
    # Mirrors getVideoNativeHLS naming: final "<name>.<container>" is written to
    # "<name>.tmp.ts" while recording. Returns None when not applicable.
    if not getattr(streamer, 'recording', False):
        return None
    recording_name = getattr(streamer, 'recording_filename', None)
    if not recording_name:
        return None
    base, _ext = os.path.splitext(recording_name)
    tmp_name = base + '.tmp.ts'
    return os.path.join(streamer.outputFolder, tmp_name)


def _streamer_summary(streamer):
    tmp_path = _live_tmp_path(streamer)
    return {
        'site': streamer.site,
        'siteslug': streamer.siteslug,
        'username': streamer.username,
        'url': streamer.url,
        'monitorState': getattr(streamer, 'monitorState', 'active'),
        'running': streamer.running,
        'status': _status_key(streamer.sc),
        'statusText': streamer.status(),
        'recording': streamer.recording,
        'liveAvailable': bool(tmp_path and os.path.exists(tmp_path)),
        'recordingFilename': getattr(streamer, 'recording_filename', None),
        'recordingBytes': getattr(streamer, 'recording_bytes', 0),
        'videoCount': len(streamer.video_files),
        'totalSize': streamer.video_files_total_size,
        'lastCheckTs': getattr(streamer, 'lastCheckTs', None),
    }


def _json(payload, status_code=200):
    return Response(json.dumps(payload), status=status_code, mimetype='application/json')


def _download_dir_payload():
    raw = parameters.get_download_dir()
    abspath = os.path.abspath(os.path.expanduser(raw))
    usage = OOSDetector.space_usage()
    return {
        'path': abspath,
        'raw': raw,
        'diskSpace': {
            'total': usage.total,
            'free': usage.free,
            'used': usage.total - usage.free,
            'freeHuman': human_file_size(usage.free),
            'totalHuman': human_file_size(usage.total),
        },
    }


def _validate_download_dir(raw):
    # Returns (abspath, None) on success or (None, (code, message)) on failure.
    if not isinstance(raw, str) or not raw.strip():
        return None, ('empty', 'Path must not be empty')
    candidate = os.path.abspath(os.path.expanduser(raw.strip()))
    try:
        os.makedirs(candidate, exist_ok=True)
    except Exception as e:
        return None, ('create_failed', f'Cannot create directory: {e}')
    if not os.path.isdir(candidate):
        return None, ('not_dir', 'Path is not a directory')
    probe = os.path.join(candidate, '.strmntr_write_test')
    try:
        with open(probe, 'w') as f:
            f.write('')
        os.remove(probe)
    except Exception:
        return None, ('not_writable', 'Directory is not writable')
    return candidate, None


# Video thumbnail + duration extraction (NB6). Frames and durations are cached
# under "<download dir>/.thumbs/" keyed by absolute source path + mtime, so a
# changed/re-recorded file regenerates automatically and status polls never
# re-run ffprobe on the whole library. The cache dir lives next to the streamer
# folders but is never scanned as a recording (cache_file_list only reads a
# streamer's own folder and skips subdirectories).
_THUMB_WIDTH = 480
_THUMB_TIMEOUT = 30  # seconds; a broken file fails ffmpeg fast, well under this
_FFPROBE_TIMEOUT = 20


def _ffprobe_bin():
    # Derive the ffprobe binary from the configured ffmpeg path so a custom
    # install location keeps working; default ('ffmpeg' on PATH) -> 'ffprobe'.
    directory = os.path.dirname(FFMPEG_PATH)
    base = os.path.basename(FFMPEG_PATH)
    base = 'ffprobe' + base[len('ffmpeg'):] if base.startswith('ffmpeg') else 'ffprobe'
    return os.path.join(directory, base) if directory else base


def _thumbs_dir():
    # Absolute so send_file (which resolves relative paths against the Flask app
    # root, not the CWD) reads the cached frame correctly.
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs'))


def _cache_key(abs_path, mtime):
    return hashlib.sha1(f'{abs_path}:{mtime}'.encode('utf-8')).hexdigest()


def _thumb_cache_path(abs_path, mtime):
    return os.path.join(_thumbs_dir(), _cache_key(abs_path, mtime) + '.jpg')


def _meta_cache_path(abs_path, mtime):
    return os.path.join(_thumbs_dir(), _cache_key(abs_path, mtime) + '.json')


def _probe_duration(abs_path):
    # Returns duration in seconds (float) or None when the file is unreadable.
    try:
        result = subprocess.run(
            [_ffprobe_bin(), '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', abs_path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=_FFPROBE_TIMEOUT)
        duration = float(result.stdout.decode('utf-8', 'ignore').strip())
        if duration > 0:
            return duration
    except Exception:
        pass
    return None


def _get_duration_cached(abs_path, mtime):
    meta_path = _meta_cache_path(abs_path, mtime)
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                return json.load(f).get('duration')
        except Exception:
            pass
    duration = _probe_duration(abs_path)
    try:  # cache failures too (duration=None) to avoid re-probing broken files
        os.makedirs(_thumbs_dir(), exist_ok=True)
        tmp = meta_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'duration': duration}, f)
        os.replace(tmp, meta_path)
    except Exception:
        pass
    return duration


def _generate_thumb(abs_path, out_path, duration=None):
    # Extract one frame ~10% into the video (fallback 5s) scaled to _THUMB_WIDTH.
    # Writes atomically via a temp file; returns True on success.
    if duration is None:
        duration = _probe_duration(abs_path)
    seek = 5.0
    if duration and duration > 0:
        seek = min(max(duration * 0.1, 1.0), max(duration - 0.5, 0.0))
    tmp = out_path + '.tmp.jpg'
    try:
        os.makedirs(_thumbs_dir(), exist_ok=True)
        result = subprocess.run(
            [FFMPEG_PATH, '-y', '-ss', str(seek), '-i', abs_path,
             '-frames:v', '1', '-an', '-vf', f'scale={_THUMB_WIDTH}:-2',
             '-q:v', '3', tmp],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=_THUMB_TIMEOUT)
        if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out_path)
            return True
    except Exception:
        pass
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return False


# Real on-site model avatar (NB12). "Show the model the way their site shows
# them." The URL is pulled from the bot's live lastInfo via a small per-site
# extractor; the image is proxied through the bot's own session (so cookies /
# proxy / referer that defeat hotlink+CORS apply) and cached to
# "<download dir>/.thumbs/avatars/" keyed by site:user with a TTL. When the bot
# is offline (lastInfo empty) a previously cached avatar is still served; sites
# with no known extractor, or any failure, 404 so the frontend falls back to its
# generated placeholder.
_AVATAR_TTL = 24 * 60 * 60  # seconds; refetch at most once a day
_AVATAR_TIMEOUT = 15
_AVATAR_MAX_BYTES = 8 * 1024 * 1024


def _avatar_from_stripchat(info):
    # getStatus stores {'model': <user dict>, ...cam}. The user dict carries the
    # profile picture (avatarUrl) and a live snapshot (previewUrl).
    model = info.get('model') if isinstance(info, dict) else None
    for src in (model, info):
        if isinstance(src, dict):
            for key in ('avatarUrl', 'previewUrl', 'snapshotUrl'):
                url = src.get(key)
                if url:
                    return url
    return None


def _avatar_from_chaturbate(info):
    if isinstance(info, dict):
        for key in ('img', 'image_url', 'imageUrl'):
            url = info.get(key)
            if url:
                return url
    return None


# Keyed by Bot.siteslug. Unknown sites intentionally absent -> no avatar.
_AVATAR_EXTRACTORS = {
    'SC': _avatar_from_stripchat,
    'SCVR': _avatar_from_stripchat,
    'CB': _avatar_from_chaturbate,
}


def _avatar_source_url(streamer):
    extractor = _AVATAR_EXTRACTORS.get(getattr(streamer, 'siteslug', None))
    if extractor is None:
        return None
    info = getattr(streamer, 'lastInfo', None)
    if not info:
        return None
    url = extractor(info)
    if not url:
        return None
    if url.startswith('//'):
        url = 'https:' + url
    return url


def _avatars_dir():
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs', 'avatars'))


def _avatar_cache_path(site, user):
    key = hashlib.sha1(f'{site}:{user}'.encode('utf-8')).hexdigest()
    return os.path.join(_avatars_dir(), key + '.img')


def _sniff_image_mime(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return 'image/jpeg'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def _download_avatar(streamer, url, out_path):
    # Fetch through the bot's session (its headers/cookies/proxy) and cache
    # atomically. Returns True on success. Only image/* responses are accepted.
    try:
        os.makedirs(_avatars_dir(), exist_ok=True)
        r = streamer.session.get(
            url, headers=streamer.headers, cookies=streamer.cookies,
            timeout=_AVATAR_TIMEOUT)
        if r.status_code != 200 or not r.content:
            return False
        if len(r.content) > _AVATAR_MAX_BYTES:
            return False
        ctype = r.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if ctype and not ctype.startswith('image/'):
            return False
        tmp = out_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(r.content)
        os.replace(tmp, out_path)
        return True
    except Exception:
        return False


# Real site favicon for the site badge (NB11). "Show each site the way it brands
# itself." The domain is derived from the site's own getWebsiteURL (a live
# streamer's when available, else the site class with a placeholder instance) and
# the icon is cached to "<download dir>/.thumbs/favicons/" keyed by siteslug with
# a long TTL. Fetch order: /favicon.ico -> <link rel=icon> in the homepage HTML
# -> Google's favicon service as a last resort. Unknown sites or a total failure
# 404 so the frontend falls back to its colored dot + abbreviation.
_FAVICON_TTL = 7 * 24 * 60 * 60  # seconds; refetch at most once a week
_FAVICON_TIMEOUT = 10
_FAVICON_MAX_BYTES = 1024 * 1024


def _favicons_dir():
    return os.path.abspath(os.path.join(parameters.get_download_dir(), '.thumbs', 'favicons'))


def _favicon_cache_path(site):
    key = hashlib.sha1(site.encode('utf-8')).hexdigest()
    return os.path.join(_favicons_dir(), key + '.img')


def _favicon_mime(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return 'image/x-icon'
    if head.startswith(b'\x00\x00\x01\x00'):
        return 'image/x-icon'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.lstrip()[:4] == b'<svg' or head.startswith(b'<?xml'):
        return 'image/svg+xml'
    return 'image/x-icon'


def _site_domain(manager, site_cls):
    # Prefer a live streamer's URL (it already knows room_id and such); otherwise
    # ask the site class with a placeholder instance. Returns a bare host or None.
    url = None
    for s in manager.streamers:
        if type(s) is site_cls:
            try:
                url = s.getWebsiteURL()
            except Exception:
                url = None
            if url:
                break
    if not url:
        try:
            url = site_cls.getWebsiteURL(SimpleNamespace(username='_', room_id=None))
        except Exception:
            url = None
    if not url:
        return None
    return urlparse(url).netloc or None


def _favicon_http_get(url):
    r = requests.get(
        url, headers={'User-Agent': parameters.HTTP_USER_AGENT}, timeout=_FAVICON_TIMEOUT)
    if r.status_code != 200 or not r.content:
        return None, None
    ctype = r.headers.get('Content-Type', '').split(';')[0].strip().lower()
    return r.content, ctype


def _favicon_from_html(domain):
    try:
        content, ctype = _favicon_http_get('https://' + domain + '/')
    except Exception:
        return None
    if not content or (ctype and not ctype.startswith('text/html')):
        return None
    html = content.decode('utf-8', 'ignore')
    tag = re.search(r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*>', html, re.I)
    if not tag:
        return None
    href = re.search(r'href=["\']([^"\']+)["\']', tag.group(0), re.I)
    if not href:
        return None
    return urljoin('https://' + domain + '/', href.group(1))


def _fetch_favicon(domain):
    # Returns raw image bytes or None. Tries the site's own icon first, then a
    # Google favicon-service fallback so a recognizable icon is (almost) always
    # returned. Centralized here so tests can monkeypatch a single function.
    candidates = ['https://' + domain + '/favicon.ico']
    try:
        html_icon = _favicon_from_html(domain)
    except Exception:
        html_icon = None
    if html_icon:
        candidates.insert(0, html_icon)
    candidates.append('https://www.google.com/s2/favicons?sz=64&domain=' + domain)
    for url in candidates:
        try:
            content, ctype = _favicon_http_get(url)
        except Exception:
            continue
        if not content or len(content) > _FAVICON_MAX_BYTES:
            continue
        if ctype and not (ctype.startswith('image/') or ctype == 'application/octet-stream'):
            continue
        return content
    return None


def _download_favicon(domain, out_path):
    content = _fetch_favicon(domain)
    if not content:
        return False
    try:
        os.makedirs(_favicons_dir(), exist_ok=True)
        tmp = out_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(content)
        os.replace(tmp, out_path)
        return True
    except Exception:
        return False


def register_api_v2(app, manager, login_required):
    @app.route('/api/v2/streamers', methods=['GET'])
    @login_required
    def api_v2_streamers():
        return _json([_streamer_summary(s) for s in manager.streamers])

    @app.route('/api/v2/streamers/<site>/<user>', methods=['GET'])
    @login_required
    def api_v2_streamer_detail(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        streamer.cache_file_list()
        recording_name = getattr(streamer, 'recording_filename', None)
        videos = []
        for video in streamer.video_files:
            is_recording = bool(streamer.recording and video.filename == recording_name)
            mtime = int(video._stat.st_mtime)
            # Skip probing the file the recorder is still writing.
            duration = None if is_recording else _get_duration_cached(video.abs_path, mtime)
            videos.append({
                'filename': video.filename,
                'size': video.filesize,
                'mtime': mtime,
                'duration': duration,
                'url': f'/video/{streamer.username}/{streamer.siteslug}/{video.filename}',
                'recording': is_recording,
            })
        videos.sort(key=lambda v: v['mtime'], reverse=True)
        detail = _streamer_summary(streamer)
        detail['videos'] = videos
        return _json(detail)

    @app.route('/api/v2/streamers/<site>/<user>/live', methods=['GET'])
    @login_required
    def api_v2_streamer_live(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        if not getattr(streamer, 'recording', False):
            return _json({'error': 'Not recording'}, 404)
        tmp_path = _live_tmp_path(streamer)
        if not tmp_path or not os.path.exists(tmp_path):
            return _json({'error': 'Live stream not available'}, 404)

        # Detect the container actually on disk. StripChat/CMAF writes fragmented
        # MP4 (ftyp/moov + moof/mdat) into the .tmp.ts; other sites write raw
        # MPEG-TS. Serve each with the correct mimetype and start boundary.
        try:
            with open(tmp_path, 'rb') as f:
                size = os.fstat(f.fileno()).st_size
                header = f.read(12)
                is_fmp4 = header[4:8] == b'ftyp'
                init_bytes = b''
                if is_fmp4:
                    mimetype = 'video/mp4'
                    init_end = _fmp4_init_end(f)
                    if init_end:
                        f.seek(0)
                        init_bytes = f.read(init_end)
                    win_base = max(init_end or 0, size - _LIVE_TAIL_BYTES)
                    f.seek(win_base)
                    window = f.read()
                    start = _fmp4_last_moof(window, win_base)
                    if start is None:
                        start = init_end or 0
                else:
                    mimetype = 'video/mp2t'
                    start = max(0, size - _LIVE_TAIL_BYTES)
                    start -= start % _TS_PACKET_SIZE  # align to a TS packet
        except OSError:
            return _json({'error': 'Live stream not available'}, 404)

        def generate():
            try:
                if init_bytes:
                    yield init_bytes
                with open(tmp_path, 'rb') as f:
                    f.seek(start)
                    idle = 0
                    while True:
                        chunk = f.read(_LIVE_READ_CHUNK)
                        if chunk:
                            idle = 0
                            yield chunk
                            continue
                        # Caught up to the write head: stop if recording ended or
                        # the temp file vanished (post-processing removed it).
                        if not getattr(streamer, 'recording', False):
                            break
                        if not os.path.exists(tmp_path):
                            break
                        idle += 1
                        if idle >= _LIVE_MAX_IDLE_ITERS:
                            break
                        sleep(_LIVE_POLL_INTERVAL)
            except (GeneratorExit, OSError):
                return

        headers = {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
        return Response(generate(), mimetype=mimetype, headers=headers)

    @app.route('/api/v2/streamers/<site>/<user>/videos/<path:filename>', methods=['DELETE'])
    @login_required
    def api_v2_delete_video(site, user, filename):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
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
        streamer.cache_file_list()
        return _json({'ok': True})

    @app.route('/api/v2/streamers/<site>/<user>/videos/<path:filename>/thumb', methods=['GET'])
    @login_required
    def api_v2_video_thumb(site, user, filename):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        # No thumbnail for the file the recorder is still writing.
        if streamer.recording and filename == getattr(streamer, 'recording_filename', None):
            return _json({'error': 'Recording in progress'}, 404)
        streamer.cache_file_list()
        match = next((v for v in streamer.video_files if v.filename == filename), None)
        if match is None:
            return _json({'error': 'Video not found'}, 404)
        mtime = int(match._stat.st_mtime)
        thumb_path = _thumb_cache_path(match.abs_path, mtime)
        if not os.path.exists(thumb_path):
            duration = _get_duration_cached(match.abs_path, mtime)
            if not _generate_thumb(match.abs_path, thumb_path, duration):
                # Broken/unparseable file: 404 so the frontend falls back to its
                # generated placeholder image.
                return _json({'error': 'Thumbnail unavailable'}, 404)
        return send_file(thumb_path, mimetype='image/jpeg')

    @app.route('/api/v2/streamers/<site>/<user>/avatar', methods=['GET'])
    @login_required
    def api_v2_streamer_avatar(site, user):
        streamer = manager.getStreamer(user, site)
        if streamer is None:
            return _json({'error': 'Streamer not found'}, 404)
        cache_path = _avatar_cache_path(streamer.site, streamer.username)
        cached = os.path.exists(cache_path)
        fresh = cached and (time() - os.path.getmtime(cache_path) < _AVATAR_TTL)
        if fresh:
            return send_file(cache_path, mimetype=_sniff_image_mime(cache_path))
        url = _avatar_source_url(streamer)
        if url and _download_avatar(streamer, url, cache_path):
            return send_file(cache_path, mimetype=_sniff_image_mime(cache_path))
        if cached:  # stale but present, and no live source right now — serve it
            return send_file(cache_path, mimetype=_sniff_image_mime(cache_path))
        # No extractor / offline with no prior cache / download failed: 404 so the
        # frontend falls back to its generated placeholder avatar.
        return _json({'error': 'Avatar unavailable'}, 404)

    @app.route('/api/v2/sites/<site>/favicon', methods=['GET'])
    @login_required
    def api_v2_site_favicon(site):
        site_cls = Bot.str2site(site)
        if site_cls is None:
            return _json({'error': 'Unknown site'}, 404)
        cache_path = _favicon_cache_path(site_cls.siteslug)
        cached = os.path.exists(cache_path)
        fresh = cached and (time() - os.path.getmtime(cache_path) < _FAVICON_TTL)
        if fresh:
            return send_file(cache_path, mimetype=_favicon_mime(cache_path))
        domain = _site_domain(manager, site_cls)
        if domain and _download_favicon(domain, cache_path):
            return send_file(cache_path, mimetype=_favicon_mime(cache_path))
        if cached:  # stale but present, and no fresh fetch right now — serve it
            return send_file(cache_path, mimetype=_favicon_mime(cache_path))
        # Unknown domain / fetch failed with no prior cache: 404 so the frontend
        # falls back to its colored dot + abbreviation.
        return _json({'error': 'Favicon unavailable'}, 404)

    @app.route('/api/v2/basesettings', methods=['GET'])
    @login_required
    def api_v2_basesettings():
        sites = {site.siteslug: site.site for site in LOADED_SITES}
        statuses = {status.value: Bot.status_messages[status] for status in Status}
        usage = OOSDetector.space_usage()
        return _json({
            'sites': sites,
            'status': statuses,
            'webListFrequency': WEB_LIST_FREQUENCY,
            'diskSpace': {
                'total': usage.total,
                'free': usage.free,
                'used': usage.total - usage.free,
                'percentageFree': round(usage.free / usage.total * 100, 3),
                'freeHuman': human_file_size(usage.free),
                'totalHuman': human_file_size(usage.total),
            },
        })

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

    @app.route('/api/v2/history', methods=['GET'])
    @login_required
    def api_v2_history():
        return _json(config.load_history())

    @app.route('/api/v2/history/<int:idx>/readd', methods=['POST'])
    @login_required
    def api_v2_history_readd(idx):
        history = config.load_history()
        if idx < 0 or idx >= len(history):
            return _json({'error': 'History entry not found'}, 404)
        entry = history[idx]
        username = entry.get('username', '')
        site = entry.get('site', '')
        existing = manager.getStreamer(username, site)
        res = manager.do_add(existing, username, site)
        if not res.startswith('Added') and res != 'Streamer already exists':
            return _json({'error': res}, 400)
        history.pop(idx)
        config.save_history(history)
        streamer = manager.getStreamer(username, site)
        if streamer is None:
            return _json({'ok': True})
        return _json(_streamer_summary(streamer), 201)

    @app.route('/api/v2/history/<int:idx>', methods=['DELETE'])
    @login_required
    def api_v2_history_delete(idx):
        history = config.load_history()
        if idx < 0 or idx >= len(history):
            return _json({'error': 'History entry not found'}, 404)
        history.pop(idx)
        config.save_history(history)
        return _json({'ok': True})

    @app.route('/api/v2/settings/download-dir', methods=['GET'])
    @login_required
    def api_v2_get_download_dir():
        return _json(_download_dir_payload())

    @app.route('/api/v2/settings/download-dir', methods=['PUT', 'POST'])
    @login_required
    def api_v2_set_download_dir():
        data = request.get_json(silent=True) or {}
        candidate, err = _validate_download_dir(data.get('path', ''))
        if err:
            code, message = err
            return _json({'error': message, 'code': code}, 400)
        # New recordings pick up the change immediately (bot.py reads the dir at
        # write time); existing files in the old directory are not migrated.
        parameters.set_download_dir(candidate)
        parameters.persist_download_dir(candidate)
        return _json(_download_dir_payload())
