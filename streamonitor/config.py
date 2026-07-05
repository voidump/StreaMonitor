import json
import os
import sys
import time

from streamonitor.bot import Bot
from streamonitor.log import Logger

logger = Logger('[CONFIG]').get_logger()
config_loc = "config.json"


def load_config():
    try:
        with open(config_loc, "r+") as f:
            return json.load(f)
    except FileNotFoundError:
        with open(config_loc, "w+") as f:
            json.dump([], f, indent=4)
            return []
    except Exception as e:
        print(e)
        sys.exit(1)


def save_config(config):
    # Write to a temp file and rename so a crash mid-write can't corrupt the
    # existing config.
    tmp_loc = config_loc + ".tmp"
    try:
        with open(tmp_loc, "w") as f:
            json.dump(config, f, indent=4)
        os.replace(tmp_loc, config_loc)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        try:
            os.remove(tmp_loc)
        except OSError:
            pass
        return False


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
