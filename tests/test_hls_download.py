from unittest import mock

import streamonitor.downloaders.hls as hls_mod


class FakeResponse:
    def __init__(self, status_code=200, content=b''):
        self.status_code = status_code
        self.content = content


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.playlist_fetches = 0

    def get(self, url, **kwargs):
        if url.endswith('.m3u8'):
            self.playlist_fetches += 1
            return self.handler(self.playlist_fetches)
        return FakeResponse(200, b'x' * 100)


class FakeBot:
    headers = {}
    cookies = None

    def __init__(self):
        self.logger = mock.Mock()
        self.stopDownload = None
        self.recording_bytes = 0
        self.recording_segments = 0
        self.quitting = False

    def debug(self, *a, **kw):
        pass

    def update_recording_progress(self, total):
        pass

    def reset_recording_progress(self):
        pass


class FakeFFmpeg:
    def __init__(self, executable=None, inputs=None, outputs=None):
        self.outputs = outputs

    def run(self, stdout=None, stderr=None):
        with open(list(self.outputs)[0], 'wb') as f:
            f.write(b'y' * 2048)


def _run(monkeypatch, tmp_path, handler, quitting=False, ffmpeg=FakeFFmpeg):
    session = FakeSession(handler)
    monkeypatch.setattr(hls_mod.requests, 'Session', lambda: session)
    monkeypatch.setattr(hls_mod, 'sleep', lambda *_: None)
    monkeypatch.setattr(hls_mod, 'FFmpeg', ffmpeg)
    monkeypatch.setattr(hls_mod, 'remove_undersized_videos', lambda paths, logger: None)
    bot = FakeBot()
    bot.quitting = quitting
    filename = str(tmp_path / 'alice-20260823-120000.mp4')
    result = hls_mod.getVideoNativeHLS(bot, 'http://example.invalid/playlist.m3u8', filename)
    return session, result


def test_transient_5xx_is_retried_not_treated_as_stream_end(monkeypatch, tmp_path):
    # A CDN 500 with an HTML error body used to decode fine, parse to a
    # 0-segment playlist and end the recording as if the stream were over —
    # one blip cut the recording. Non-200 must go through the retry counter.
    session, result = _run(
        monkeypatch, tmp_path,
        lambda n: FakeResponse(500, b'<html>Internal Server Error</html>'))
    assert session.playlist_fetches >= 15, \
        'a single 5xx must not end the recording round'
    assert result is False  # nothing was ever downloaded


def test_malformed_playlist_is_retried(monkeypatch, tmp_path):
    def boom(n):
        raise_next = FakeResponse(200, b'#EXTM3U\n#EXT-X-VERSION:3\n')
        return raise_next

    monkeypatch.setattr(hls_mod.m3u8, 'loads',
                        mock.Mock(side_effect=ValueError('bad playlist')))
    session, result = _run(monkeypatch, tmp_path, boom)
    assert session.playlist_fetches >= 15
    assert result is False


def test_shutdown_skips_remux_and_keeps_temp_file(monkeypatch, tmp_path):
    # During process shutdown a size-proportional remux could outlive
    # CleanExit's deadline and get the process force-killed mid-remux; the
    # temp file is left for the startup repair chain instead.
    remux_calls = []

    class RecordingFFmpeg(FakeFFmpeg):
        def run(self, stdout=None, stderr=None):
            remux_calls.append(1)
            super().run(stdout=stdout, stderr=stderr)

    playlist = (b'#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.0,\nseg-1.ts\n')
    session, result = _run(monkeypatch, tmp_path,
                           lambda n: FakeResponse(200, playlist),
                           quitting=True, ffmpeg=RecordingFFmpeg)
    assert result is True
    assert remux_calls == []
    tmp_files = [p for p in tmp_path.iterdir() if '.tmp.' in p.name]
    assert len(tmp_files) == 1, 'temp file must be left for startup repair'


def test_frozen_playlist_hits_the_stall_limit(monkeypatch, tmp_path):
    # 200 + never any new segment: the loop used to spin sleep(2) forever with
    # no exit condition, leaving the recording thread hung indefinitely.
    playlist = (b'#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:2.0,\nseg-1.ts\n')
    session, result = _run(monkeypatch, tmp_path, lambda n: FakeResponse(200, playlist))
    assert result is True  # one segment downloaded, then the stall limit ends it
    assert session.playlist_fetches <= hls_mod.MAX_STALL_ROUNDS + 3
