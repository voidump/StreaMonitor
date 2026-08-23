import base64
import json
import threading
import time
from unittest import mock

from ffmpy import FFRuntimeError

import parameters
import streamonitor.config as config
import streamonitor.manager as manager_mod
import streamonitor.managers.httpmanager.api_v2 as api_v2
import streamonitor.temp_repair as temp_repair
from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.manager import Manager
from streamonitor.managers.httpmanager import HTTPManager


AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin").decode()}


class SlowFake:
    site = 'SlowFake'
    siteslug = 'SLF'

    def __init__(self, username):
        self.username = username
        self.site = 'SlowFake'
        self.siteslug = 'SLF'
        self.logger = mock.Mock(handlers=[])
        self.outputFolder = '/nonexistent'

    def start(self):
        pass

    def restart(self):
        pass

    def export(self):
        return {'site': self.site, 'username': self.username}


def test_concurrent_duplicate_add_creates_single_entry(monkeypatch):
    # Both adds resolve getStreamer to None outside the lock; the in-lock
    # re-check must reject the loser instead of double-recording the streamer.
    monkeypatch.setattr(manager_mod.config, 'save_config', lambda data: True)
    barrier = threading.Barrier(2)

    def slow_create(username, site):
        barrier.wait(timeout=5)
        time.sleep(0.05)
        return SlowFake(username)

    monkeypatch.setattr(manager_mod.Bot, 'createInstance',
                        staticmethod(slow_create))
    m = Manager([])
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(m.do_add(None, 'dupe', 'SlowFake')))
        for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(m.streamers) == 1
    assert sorted(r.startswith('Added') for r in results) == [False, True]


def test_temp_repair_gives_up_after_repeated_failures(tmp_path, monkeypatch):
    # A genuinely unrepairable file must not burn a full-size remux on every
    # scan cycle forever (disk churn in exactly the low-disk scenario).
    monkeypatch.setattr(temp_repair.parameters, 'get_download_dir',
                        lambda: str(tmp_path / 'downloads'))
    monkeypatch.setattr(temp_repair, 'remove_undersized_videos',
                        lambda paths, logger: None)
    monkeypatch.setattr(temp_repair, 'record_repaired_file', lambda path: None)
    monkeypatch.setattr(temp_repair, '_failed_attempts', {})
    runs = []

    class CountingFailingFFmpeg:
        def __init__(self, executable=None, inputs=None, outputs=None):
            pass

        def run(self, stdout=None, stderr=None):
            runs.append(1)
            raise FFRuntimeError('ffmpeg', 1, b'', b'')

    monkeypatch.setattr(temp_repair, 'FFmpeg', CountingFailingFFmpeg)
    folder = tmp_path / 'downloads' / 'alice [CB]'
    folder.mkdir(parents=True)
    tmp_file = folder / 'alice-1.tmp.ts'
    tmp_file.write_bytes(b'0' * 512)
    for _ in range(6):
        temp_repair._scan_and_repair()
    assert len(runs) == temp_repair.MAX_FAILURES_PER_FILE
    assert tmp_file.exists()
    # A rewritten (changed) file gets fresh attempts.
    tmp_file.write_bytes(b'0' * 1024)
    temp_repair._scan_and_repair()
    assert len(runs) == temp_repair.MAX_FAILURES_PER_FILE + 1


class NormFake(Bot):
    site = 'NormFake'
    siteslug = 'NF'

    @classmethod
    def fromConfig(cls, data):
        raise ConnectionError('down')


def test_unloaded_merge_normalizes_site_aliases(tmp_path, monkeypatch):
    # 'normfake' (raw config) and 'NormFake' (canonical export) are the same
    # streamer; the merge key must normalize or the entry gets duplicated and
    # double-recorded after the next restart.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, 'config_loc', 'config.json')
    LOADED_SITES.add(NormFake)
    try:
        config.save_config([{'site': 'normfake', 'username': 'foo'}])
        config.loadStreamers()
        config.save_config([{'site': 'NormFake', 'username': 'foo', 'running': True}])
        persisted = json.loads((tmp_path / 'config.json').read_text())
        assert len(persisted) == 1
        assert persisted[0]['site'] == 'NormFake'
    finally:
        LOADED_SITES.discard(NormFake)
        config.unloaded_entries.clear()


def test_live_stream_concurrency_cap(monkeypatch):
    # Each /live stream pins a WSGI worker thread; over the cap the endpoint
    # must answer 503 instead of letting viewers exhaust the pool.
    client = HTTPManager([]).create_app().test_client()

    class Exhausted:
        def acquire(self, blocking=True):
            return False

    monkeypatch.setattr(api_v2, '_live_slots', Exhausted())
    streamer = mock.Mock()
    streamer.recording = True
    monkeypatch.setattr(api_v2.os.path, 'exists', lambda p: True)
    with mock.patch.object(HTTPManager, 'getStreamer', return_value=streamer), \
            mock.patch.object(api_v2, '_live_tmp_path', return_value='/tmp/x.tmp.ts'):
        client2 = HTTPManager([]).create_app().test_client()
        response = client2.get('/api/v2/streamers/FS/alice/live', headers=AUTH)
    assert response.status_code == 503


def test_trust_proxy_uses_forwarded_ip(monkeypatch):
    monkeypatch.setattr(parameters, 'WEBSERVER_PASSWORD', 'admin')
    monkeypatch.setattr(parameters, 'WEBSERVER_TRUST_PROXY', True)
    client = HTTPManager([]).create_app().test_client()
    # Failures from two different forwarded clients must not share a bucket.
    import streamonitor.managers.httpmanager.httpmanager as hm
    for i in range(hm.LOGIN_MAX_FAILURES):
        client.post('/login', json={'username': 'admin', 'password': 'x'},
                    headers={'X-Forwarded-For': '203.0.113.7'})
    locked = client.post('/login', json={'username': 'admin', 'password': 'admin'},
                         headers={'X-Forwarded-For': '203.0.113.7'})
    assert locked.status_code == 429
    other = client.post('/login', json={'username': 'admin', 'password': 'admin'},
                        headers={'X-Forwarded-For': '198.51.100.9'})
    assert other.status_code == 200


def test_download_dir_rejects_control_characters(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    saved_dir = parameters.get_download_dir()
    client = HTTPManager([]).create_app().test_client()
    try:
        response = client.put('/api/v2/settings/download-dir',
                              json={'path': 'down\nloads'}, headers=AUTH)
        assert response.status_code == 400
        # The in-memory setting must not have changed on a rejected request.
        assert parameters.get_download_dir() == saved_dir
    finally:
        parameters.set_download_dir(saved_dir)
