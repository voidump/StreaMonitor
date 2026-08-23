import json

import streamonitor.config as config
from streamonitor.bot import Bot, LOADED_SITES


class BoomSite(Bot):
    site = 'BoomSite'
    siteslug = 'BOOM'

    @classmethod
    def fromConfig(cls, data):
        raise ConnectionError('site unreachable during startup')


def test_failed_and_unknown_entries_survive_save(tmp_path, monkeypatch):
    # A transient failure while constructing a bot (or an entry for a site
    # this build doesn't know) used to be dropped from the in-memory list;
    # the next save_config then rewrote config.json without it — permanent,
    # silent loss of the streamer's configuration.
    monkeypatch.chdir(tmp_path)
    LOADED_SITES.add(BoomSite)
    try:
        entries = [
            {'site': 'BoomSite', 'username': 'alice', 'running': True},
            {'site': 'NoSuchSite', 'username': 'bob', 'running': True},
        ]
        config.save_config(entries)

        streamers = config.loadStreamers()
        assert streamers == []

        # A later save (e.g. after any web/CLI command) must keep both entries.
        assert config.save_config([]) is True
        persisted = json.loads((tmp_path / 'config.json').read_text())
        names = {(e['site'], e['username']) for e in persisted}
        assert names == {('BoomSite', 'alice'), ('NoSuchSite', 'bob')}
    finally:
        LOADED_SITES.discard(BoomSite)
        config.unloaded_entries.clear()


def test_loaded_entries_are_not_duplicated_by_the_merge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    LOADED_SITES.add(BoomSite)
    try:
        config.save_config([{'site': 'BoomSite', 'username': 'alice'}])
        config.loadStreamers()
        # The same identity coming from the live list wins over the preserved
        # raw entry.
        config.save_config([{'site': 'BoomSite', 'username': 'alice', 'running': False}])
        persisted = json.loads((tmp_path / 'config.json').read_text())
        assert persisted == [{'site': 'BoomSite', 'username': 'alice', 'running': False}]
    finally:
        LOADED_SITES.discard(BoomSite)
        config.unloaded_entries.clear()
