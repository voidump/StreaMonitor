"""Watch-while-recording: stream the tail of the file the recorder is
writing, with a hard cap on concurrent viewer streams."""
import os
import threading
from time import sleep

from flask import Response

from .common import _json, _live_tmp_path
from .containers import _TS_PACKET_SIZE, _fmp4_init_end, _fmp4_last_moof

# Live view (watch-while-recording) tuning. We start the tail a bit before the
# write head so latency stays low. For raw MPEG-TS the start is aligned to a
# 188-byte packet boundary; for fragmented MP4 (what StripChat/CMAF actually
# writes into the .tmp.ts) we prepend the init segment and start at a recent
# fragment boundary. These are module-level so tests can shrink them.
_LIVE_TAIL_BYTES = 1 << 20  # ~1 MB backoff from the write head
_LIVE_READ_CHUNK = 64 * 1024
_LIVE_POLL_INTERVAL = 0.5  # seconds between reads when caught up to the tail
_LIVE_MAX_IDLE_ITERS = 60  # give up after this many empty reads (~30s)

# Cap on concurrent /live viewer streams. Each stream holds a WSGI worker
# thread for its whole lifetime, so without a cap a handful of open tabs could
# exhaust the (waitress) thread pool and hang the whole console, login page
# included.
LIVE_MAX_CONCURRENT = 4
_live_slots = threading.BoundedSemaphore(LIVE_MAX_CONCURRENT)


def register(app, manager, login_required):
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

        if not _live_slots.acquire(blocking=False):
            return _json({'error': 'Too many live streams open'}, 503)
        release = [_live_slots.release]

        def _release_once():
            if release:
                release.pop()()

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
            _release_once()
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
            finally:
                _release_once()

        headers = {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
        response = Response(generate(), mimetype=mimetype, headers=headers)
        # call_on_close is the WSGI-guaranteed cleanup; the generator's finally
        # alone would leak the slot if the response were closed before the
        # first iteration. _release_once makes the two paths idempotent.
        response.call_on_close(_release_once)
        return response
