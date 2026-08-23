import json
import os
import threading
import time

import parameters
from streamonitor.bot import Bot
from streamonitor.log import Logger
from streamonitor.utils.atomic_file import atomic_write_json

logger = Logger('[CONFIG]').get_logger()
config_loc = parameters.data_path("config.json")
history_loc = parameters.data_path("history.json")

# Raw config entries whose bot could not be constructed at load time (site
# unreachable, unknown site name). save_config merges them back so a transient
# startup failure can never silently erase an entry from config.json.
unloaded_entries = []


def _entry_key(entry):
    # Dedup key for the merge, normalized the same way loading normalizes:
    # str2site accepts aliases/any case while export writes the canonical
    # name, so a raw comparison would let 'chaturbate' and 'Chaturbate'
    # coexist and get double-recorded after the next restart.
    site = entry.get('site') or ''
    site_cls = Bot.str2site(site) if site else None
    return (site_cls.site if site_cls else site.lower(), entry.get('username'))


def load_config():
    try:
        with open(config_loc, "r+") as f:
            return json.load(f)
    except FileNotFoundError:
        with open(config_loc, "w+") as f:
            json.dump([], f, indent=4)
            return []
    except Exception as e:
        # Corrupt config.json: back it up and continue with an empty list so a
        # single bad file can't prevent startup.
        backup_loc = f"{config_loc}.broken-{int(time.time())}"
        try:
            os.replace(config_loc, backup_loc)
            logger.error(f"Corrupt config ({e}); backed up to {backup_loc}, starting with empty list")
        except OSError as move_err:
            logger.error(f"Corrupt config ({e}); failed to back up ({move_err}), starting with empty list")
        return []


def save_config(config):
    # Atomic write (unique temp name + fsync + rename) so a crash mid-write
    # can't corrupt the existing config and concurrent writers can't collide.
    try:
        data = list(config)
        if unloaded_entries:
            present = {_entry_key(e) for e in data}
            for entry in unloaded_entries:
                if _entry_key(entry) not in present:
                    data.append(entry)
        atomic_write_json(config_loc, data)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def load_history():
    try:
        with open(history_loc, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"Failed to load history: {e}")
        return []


def save_history(history):
    # Same atomic write strategy as save_config.
    try:
        atomic_write_json(history_loc, history)
        return True
    except Exception as e:
        logger.error(f"Failed to save history: {e}")
        return False


# Serializes every read-modify-write of history.json: do_purge inserts here
# while the history API deletes/re-adds concurrently, and unsynchronized
# load-modify-save pairs silently overwrite each other's changes.
history_lock = threading.RLock()


def add_history_entry(entry):
    with history_lock:
        history = load_history()
        history.insert(0, entry)
        return save_history(history)


def loadStreamers():
    streamers = []
    unloaded_entries.clear()
    for streamer in load_config():
        username = streamer["username"]
        site = streamer["site"]

        bot_class = Bot.str2site(site)
        if not bot_class:
            logger.warning(f'Unknown site: {site} (user: {username}); keeping its config entry')
            unloaded_entries.append(streamer)
            continue

        try:
            streamer_bot = bot_class.fromConfig(streamer)
        except Exception as e:
            logger.error(f'Failed to load streamer {username} ({site}): {e}; keeping its config entry')
            unloaded_entries.append(streamer)
            continue

        streamers.append(streamer_bot)
        try:
            streamer_bot.start()
        except Exception as e:
            logger.error(f'Failed to start streamer {username} ({site}): {e}')
        time.sleep(0.1)
    return streamers
