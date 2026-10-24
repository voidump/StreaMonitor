"""Server-sent events: push streamer state changes to the console as they
happen, so the 30s list poll is only a fallback instead of the only way the UI
learns that a recording started or stopped."""
import json
import threading
from time import monotonic, sleep, time

from flask import Response

from .common import _json, _status_key

# Each open SSE connection pins a WSGI worker thread for its whole lifetime,
# exactly like /live. waitress runs with 32 threads and /live already reserves
# up to LIVE_MAX_CONCURRENT (4) of them, so the event stream is capped at 8 —
# one per open console tab — leaving ~20 threads for ordinary requests. Over
# the cap the endpoint answers 503 and the console just keeps polling.
EVENTS_MAX_CONCURRENT = 8
_event_slots = threading.BoundedSemaphore(EVENTS_MAX_CONCURRENT)

_EVENTS_POLL_INTERVAL = 1.0  # seconds between state diffs (cheap attribute reads)
# Heartbeat doubles as disconnect detection: waitress only notices a vanished
# client when a write fails, so a dead connection is reaped within ~20s.
_EVENTS_HEARTBEAT_INTERVAL = 20.0
# Client reconnect delay hint (EventSource honours it); the console applies its
# own backoff on top before giving up and falling back to polling.
_EVENTS_RETRY_MS = 5000


def _snapshot(manager):
    """Lightweight per-streamer state: only fields whose change the dashboard
    must reflect immediately. Byte counters are deliberately excluded — they
    move constantly and the list poll carries them."""
    snap = {}
    for s in manager.streamers:
        snap[(s.siteslug, s.username)] = {
            'site': s.siteslug,
            'user': s.username,
            'status': _status_key(s.sc),
            'running': bool(s.running),
            'recording': bool(getattr(s, 'recording', False)),
            'monitorState': getattr(s, 'monitorState', 'active'),
        }
    return snap


def _diff(prev, cur):
    changed = [entry for key, entry in cur.items() if prev.get(key) != entry]
    removed = [{'site': key[0], 'user': key[1]} for key in prev if key not in cur]
    return changed, removed


def _format(event, payload):
    return 'event: %s\ndata: %s\n\n' % (event, json.dumps(payload))


def register(app, manager, login_required):
    @app.route('/api/v2/events', methods=['GET'])
    @login_required
    def api_v2_events():
        if not _event_slots.acquire(blocking=False):
            return _json({'error': 'Too many event streams open'}, 503)
        release = [_event_slots.release]

        def _release_once():
            if release:
                release.pop()()

        def generate():
            try:
                yield 'retry: %d\n\n' % _EVENTS_RETRY_MS
                snap = _snapshot(manager)
                # Full snapshot first so a (re)connected client is immediately
                # consistent without re-pulling the whole list.
                yield _format('state', {'changed': list(snap.values()), 'removed': []})
                last_sent = monotonic()
                while True:
                    sleep(_EVENTS_POLL_INTERVAL)
                    cur = _snapshot(manager)
                    changed, removed = _diff(snap, cur)
                    snap = cur
                    if changed or removed:
                        yield _format('state', {'changed': changed, 'removed': removed})
                        last_sent = monotonic()
                    elif monotonic() - last_sent >= _EVENTS_HEARTBEAT_INTERVAL:
                        yield _format('ping', {'ts': int(time())})
                        last_sent = monotonic()
            except (GeneratorExit, OSError):
                return
            finally:
                _release_once()

        headers = {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
        response = Response(generate(), mimetype='text/event-stream', headers=headers)
        # Same idempotent cleanup as /live: call_on_close is the WSGI-guaranteed
        # path, the generator's finally covers a stream that did start.
        response.call_on_close(_release_once)
        return response
