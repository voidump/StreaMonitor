"""Site branding for the console: real model avatars (proxied through the
bot's own session) and site favicons, both cached under .thumbs/."""
import hashlib
import os
import re
import threading
from time import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse

import requests

import parameters
from streamonitor.bot import Bot

from .common import (_json, _send_image, _AVATAR_CACHE_CONTROL,
                     _FAVICON_CACHE_CONTROL)

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

# Negative cache for failed external fetches (avatars and favicons alike).
# Without it every request for a model/site whose image cannot be fetched pays
# the full connect timeout again, which is exactly how an unreachable network
# ties up request threads. Keyed by cache file path, so each cache entry (and
# each test's download dir) is independent.
_FETCH_FAIL_TTL = 5 * 60  # seconds; retry a failed fetch at most this often
_fetch_failures = {}
_fetch_failures_lock = threading.Lock()


def _fetch_recently_failed(cache_path):
    with _fetch_failures_lock:
        ts = _fetch_failures.get(cache_path)
        if ts is None:
            return False
        if time() - ts < _FETCH_FAIL_TTL:
            return True
        del _fetch_failures[cache_path]
        return False


def _mark_fetch_failed(cache_path):
    with _fetch_failures_lock:
        _fetch_failures[cache_path] = time()


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
# a long TTL. Fetch order: <link rel=icon> in the homepage HTML -> /favicon.ico.
# Unknown sites or a total failure 404 so the frontend falls back to its colored
# dot + abbreviation.
_FAVICON_TTL = 7 * 24 * 60 * 60  # seconds; refetch at most once a week
# Short enough that an unreachable host cannot hold a request thread: the whole
# fetch (two candidates) stays within a few seconds instead of tens of seconds.
_FAVICON_TIMEOUT = 3
_FAVICON_MAX_BYTES = 1024 * 1024
# Google's favicon service used to be the last-resort fallback, but where Google
# is unreachable every miss costs a full connect timeout on a request thread for
# an icon the frontend can perfectly well replace with its placeholder. Off by
# default; set to True only where Google is reachable.
_FAVICON_GOOGLE_FALLBACK = False


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
    # Never image/svg+xml: an SVG can carry script, and these bytes come from
    # third-party sites. Served as x-icon (plus nosniff/sandbox headers in
    # _send_image) an SVG simply fails to render and the frontend falls back
    # to its generated placeholder.
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
    # Returns raw image bytes or None. Tries the site's own icon, plus the
    # Google favicon service only when that fallback is enabled. Centralized
    # here so tests can monkeypatch a single function.
    candidates = ['https://' + domain + '/favicon.ico']
    try:
        html_icon = _favicon_from_html(domain)
    except Exception:
        html_icon = None
    if html_icon:
        candidates.insert(0, html_icon)
    if _FAVICON_GOOGLE_FALLBACK:
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


def register(app, manager, login_required):
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
            return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)
        # Skip the download entirely while a recent attempt is still in the
        # negative cache, so an unreachable host cannot be re-dialed on every
        # request.
        if not _fetch_recently_failed(cache_path):
            url = _avatar_source_url(streamer)
            if url:
                if _download_avatar(streamer, url, cache_path):
                    return _send_image(cache_path, _sniff_image_mime(cache_path),
                                       _AVATAR_CACHE_CONTROL)
                _mark_fetch_failed(cache_path)
        if cached:  # stale but present, and no live source right now — serve it
            return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)
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
            return _send_image(cache_path, _favicon_mime(cache_path), _FAVICON_CACHE_CONTROL)
        # Same negative cache as the avatar: a site whose icon cannot be
        # fetched is not re-dialed until the TTL expires.
        if not _fetch_recently_failed(cache_path):
            domain = _site_domain(manager, site_cls)
            if domain:
                if _download_favicon(domain, cache_path):
                    return _send_image(cache_path, _favicon_mime(cache_path),
                                       _FAVICON_CACHE_CONTROL)
                _mark_fetch_failed(cache_path)
        if cached:  # stale but present, and no fresh fetch right now — serve it
            return _send_image(cache_path, _favicon_mime(cache_path), _FAVICON_CACHE_CONTROL)
        # Unknown domain / fetch failed with no prior cache: 404 so the frontend
        # falls back to its colored dot + abbreviation.
        return _json({'error': 'Favicon unavailable'}, 404)
