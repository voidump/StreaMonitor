import json
import os
import time

from streamonitor.bot import Bot
from streamonitor.log import Logger
from streamonitor.utils.atomic_file import atomic_write_json

logger = Logger('[CONFIG]').get_logger()
config_loc = "config.json"
history_loc = "history.json"


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
        atomic_write_json(config_loc, config)
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


def add_history_entry(entry):
    history = load_history()
    history.insert(0, entry)
    return save_history(history)


def loadStreamers():
    streamers = []
    for streamer in load_config():
        username = streamer["username"]
        site = streamer["site"]

        bot_class = Bot.str2site(site)
        if not bot_class:
            logger.warning(f'Unknown site: {site} (user: {username})')
            continue

        try:
            streamer_bot = bot_class.fromConfig(streamer)
        except Exception as e:
            logger.error(f'Failed to load streamer {username} ({site}): {e}')
            continue

        streamers.append(streamer_bot)
        try:
            streamer_bot.start()
        except Exception as e:
            logger.error(f'Failed to start streamer {username} ({site}): {e}')
        time.sleep(0.1)
    return streamers
