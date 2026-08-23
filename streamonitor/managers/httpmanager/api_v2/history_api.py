"""Purged-streamer history: list, re-add, delete and the cached avatar."""
import os

from flask import request

import streamonitor.config as config

from .common import _json, _send_image, _streamer_summary, _AVATAR_CACHE_CONTROL
from .branding import _avatar_cache_path, _sniff_image_mime


def _entry_identity(entry):
    return (entry.get('site'), entry.get('username'), entry.get('deletedAt'))


def _resolve_entry(history, idx, data):
    """Resolve the entry a client addressed by index, verified against the
    identity it optionally sends. history.json is a shared list: a concurrent
    purge inserts at the front (indexes shift right) and a concurrent delete
    shrinks it (a stale index can go out of bounds), so whenever the request
    carries (site, username, deletedAt) the identity wins — including when the
    stale index no longer exists at all. Returns (idx, entry) or (None,
    response)."""
    wanted = (data.get('site'), data.get('username'), data.get('deletedAt'))
    has_identity = any(v is not None for v in wanted)
    if 0 <= idx < len(history):
        entry = history[idx]
        if has_identity:
            wanted = tuple(w if w is not None else e
                           for w, e in zip(wanted, _entry_identity(entry)))
            if wanted == _entry_identity(entry):
                return idx, entry
        else:
            return idx, entry
    elif not has_identity:
        return None, _json({'error': 'History entry not found'}, 404)
    for i, candidate in enumerate(history):
        if _entry_identity(candidate) == wanted:
            return i, candidate
    return None, _json({'error': 'History entry not found'}, 404)


def register(app, manager, login_required):
    @app.route('/api/v2/history', methods=['GET'])
    @login_required
    def api_v2_history():
        return _json(config.load_history())

    @app.route('/api/v2/history/<int:idx>/readd', methods=['POST'])
    @login_required
    def api_v2_history_readd(idx):
        # history_lock serializes the read-modify-write against concurrent
        # purges (config.add_history_entry holds the same lock).
        data = request.get_json(silent=True) or {}
        with config.history_lock:
            history = config.load_history()
            idx, entry = _resolve_entry(history, idx, data)
            if idx is None:
                return entry
        username = entry.get('username', '')
        site = entry.get('site', '')
        existing = manager.getStreamer(username, site)
        res = manager.do_add(existing, username, site)
        if not res.startswith('Added') and res != 'Streamer already exists':
            return _json({'error': res}, 400)
        with config.history_lock:
            history = config.load_history()
            history = [e for e in history if _entry_identity(e) != _entry_identity(entry)]
            config.save_history(history)
        streamer = manager.getStreamer(username, site)
        if streamer is None:
            return _json({'ok': True})
        return _json(_streamer_summary(streamer), 201)

    @app.route('/api/v2/history/<int:idx>', methods=['DELETE'])
    @login_required
    def api_v2_history_delete(idx):
        data = request.get_json(silent=True) or {}
        with config.history_lock:
            history = config.load_history()
            idx, entry = _resolve_entry(history, idx, data)
            if idx is None:
                return entry
            history.pop(idx)
            config.save_history(history)
        return _json({'ok': True})

    @app.route('/api/v2/history/<int:idx>/avatar', methods=['GET'])
    @login_required
    def api_v2_history_avatar(idx):
        # Purged streamers leave their cached avatar in .thumbs/avatars/ (do_purge
        # only removes the streamer's own folder), so the history page can still
        # show the real face. There is no live bot to refetch through, so we only
        # serve an existing cache; anything missing 404s and the frontend falls
        # back to its generated placeholder.
        history = config.load_history()
        if idx < 0 or idx >= len(history):
            return _json({'error': 'History entry not found'}, 404)
        entry = history[idx]
        cache_path = _avatar_cache_path(entry.get('site', ''), entry.get('username', ''))
        if not os.path.exists(cache_path):
            return _json({'error': 'Avatar unavailable'}, 404)
        return _send_image(cache_path, _sniff_image_mime(cache_path), _AVATAR_CACHE_CONTROL)
