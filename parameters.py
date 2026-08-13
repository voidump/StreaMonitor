import json
import os.path
import environ


env = environ.Env()
if os.path.exists('.env'):
    environ.Env.read_env('.env')


DOWNLOADS_DIR = env.str("STRMNTR_DOWNLOAD_DIR", "downloads")


def get_download_dir():
    # Read the current download directory at call time so runtime changes made
    # via the settings API take effect without restarting. Do not `from
    # parameters import DOWNLOADS_DIR` in long-lived modules; use this instead.
    return DOWNLOADS_DIR


def set_download_dir(path):
    global DOWNLOADS_DIR
    DOWNLOADS_DIR = path


def persist_download_dir(path):
    # Write STRMNTR_DOWNLOAD_DIR into the project .env, preserving other keys,
    # using the same atomic write-then-rename strategy as the config store.
    env_path = '.env'
    key = 'STRMNTR_DOWNLOAD_DIR'
    new_line = f'{key}={path}'
    lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            lines = f.read().splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(key + '=') or stripped.startswith(key + ' '):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    tmp_path = env_path + '.tmp'
    with open(tmp_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp_path, env_path)
MIN_FREE_DISK_PERCENT = env.float("STRMNTR_MIN_FREE_SPACE", 5.0)  # in %

# When free space drops below STRMNTR_OOS_CLEANUP_THRESHOLD percent (should be
# above STRMNTR_MIN_FREE_SPACE), the out-of-space detector deletes finished
# videos classified 'non-adult' in the media index (oldest first) until free
# space is back above the threshold. 'adult' and 'unknown' videos, files being
# recorded and recently modified files are never touched. If cleanup cannot
# free enough space, the original exit-on-low-space behavior still applies.
# Disabled by default; requires the media index (classification source).
OOS_CLEANUP_NON_ADULT = env.bool("STRMNTR_OOS_CLEANUP_NON_ADULT", False)
OOS_CLEANUP_THRESHOLD_PERCENT = env.float("STRMNTR_OOS_CLEANUP_THRESHOLD", 8.0)  # in %
DEBUG = env.bool("STRMNTR_DEBUG", False)

# Repair leftover temp files on startup (hls .tmp.ts / fmp4s .tmp.mp4 left behind
# by an interrupted recording). Set to False to keep the original behavior.
REPAIR_TEMP_ON_START = env.bool("STRMNTR_REPAIR_TEMP_ON_START", True)

# Also repair leftover temp files periodically at runtime, every this many
# seconds (default 30 minutes). Temp files of an in-progress recording
# (streamer recording state or a recent mtime) are never touched.
# Set to 0 to disable the periodic scan.
REPAIR_TEMP_INTERVAL = env.int("STRMNTR_REPAIR_TEMP_INTERVAL", 1800)

# SQLite media index of recorded videos, stored at <download dir>/.thumbs/media_index.db.
# A background thread runs a full scan on startup and an incremental scan every
# STRMNTR_MEDIA_INDEX_SCAN_INTERVAL seconds (skipping files being recorded).
# Set the enabled flag to False to turn the index off entirely.
MEDIA_INDEX_ENABLED = env.bool("STRMNTR_MEDIA_INDEX_ENABLED", True)
MEDIA_INDEX_SCAN_INTERVAL = env.int("STRMNTR_MEDIA_INDEX_SCAN_INTERVAL", 300)  # in seconds

# Automatically remove video files smaller than this many MB (tiny fragments
# left by an aborted stream). Applied to files just created by a finished
# recording or the startup temp repair, and by the media index scanner to
# historic files in the library. Files being recorded and .tmp. files are
# never touched. Set to 0 to disable the filter entirely.
MIN_VIDEO_SIZE_MB = env.float("STRMNTR_MIN_VIDEO_SIZE_MB", 10.0)

# Move finished videos classified 'non-adult' in the media index into a
# separate directory, keeping the "<username> [<siteslug>]" folder layout so
# the web UI keeps listing, playing and thumbnailing them after the move.
# Files being recorded and recently modified files are never moved. Disabled
# by default; requires the media index (classification source). The directory
# is resolved against the download dir unless an absolute path is given.
NONADULT_RELOCATE_ENABLED = env.bool("STRMNTR_NONADULT_RELOCATE", False)
NONADULT_DIR = env.str("STRMNTR_NONADULT_DIR", "_nonadult")


def get_nonadult_dir():
    # Resolved at call time so a runtime download-dir change is honored.
    if os.path.isabs(NONADULT_DIR):
        return NONADULT_DIR
    return os.path.join(get_download_dir(), NONADULT_DIR)


def get_min_video_size_mb():
    # Read at call time so runtime changes made via the settings API take
    # effect without restarting (same pattern as get_download_dir).
    return MIN_VIDEO_SIZE_MB


def set_min_video_size_mb(value):
    global MIN_VIDEO_SIZE_MB
    MIN_VIDEO_SIZE_MB = value


def persist_min_video_size_mb(value):
    # Write STRMNTR_MIN_VIDEO_SIZE_MB into the project .env, preserving other
    # keys, using the same atomic write-then-rename as persist_download_dir.
    env_path = '.env'
    key = 'STRMNTR_MIN_VIDEO_SIZE_MB'
    new_line = f'{key}={value:g}'
    lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            lines = f.read().splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(key + '=') or stripped.startswith(key + ' '):
            lines[i] = new_line
            found = True
            break
    if not found:
        lines.append(new_line)
    tmp_path = env_path + '.tmp'
    with open(tmp_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp_path, env_path)

# --- NSFW frame analysis (optional, disabled by default) ---
# When enabled, a frame is grabbed from the file being recorded every
# STRMNTR_NSFW_SAMPLE_INTERVAL seconds and scored by a local ONNX NSFW
# classifier; when the recording ends the video is labeled adult / non-adult /
# unknown in the media index. Requires the optional onnxruntime + numpy
# packages and a model file at STRMNTR_NSFW_MODEL_PATH (supported models are
# documented in streamonitor/nsfw_analyzer.py). While this flag is False
# (default) nothing is imported, loaded or started. When analysis is not
# possible (missing packages/model, ffmpeg errors) videos are labeled
# 'unknown' and recording is never affected.
NSFW_ENABLED = env.bool("STRMNTR_NSFW_ENABLED", False)

# Seconds between frame grabs while a recording is in progress.
NSFW_SAMPLE_INTERVAL = env.int("STRMNTR_NSFW_SAMPLE_INTERVAL", 60)

# A frame whose adult confidence (0..1) is at or above this threshold marks
# the whole video as adult; a video is non-adult only if every sampled frame
# stays below it.
NSFW_THRESHOLD = env.float("STRMNTR_NSFW_THRESHOLD", 0.7)

# Path to the ONNX classifier model file (bring your own model, it is not
# downloaded automatically; see streamonitor/nsfw_analyzer.py for options).
NSFW_MODEL_PATH = env.str("STRMNTR_NSFW_MODEL_PATH", os.path.join('models', 'nsfw-model.onnx'))

# Maximum number of frames sampled evenly across the finished file when live
# sampling produced fewer scores than this (short recordings, unseekable tails).
NSFW_MAX_SAMPLES = env.int("STRMNTR_NSFW_MAX_SAMPLES", 8)

# The camsoda bot ignores this setting in favor of a chrome useragent generated with the fake-useragent library
HTTP_USER_AGENT = env.str("STRMNTR_USER_AGENT", "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0")

# Specify the full path to the ffmpeg binary. By default, ffmpeg found on PATH is used.
FFMPEG_PATH = env.str("STRMNTR_FFMPEG_PATH", 'ffmpeg')

# You can enter a number to select a specific height.
# Use a huge number here and closest match to get the highest resolution variant
# Eg: 240, 360, 480, 720, 1080, 1440, 99999
WANTED_RESOLUTION = env.int("STRMNTR_RESOLUTION", 1080)

# Specify match type when specified height
# Possible values: exact, exact_or_least_higher, exact_or_highest_lower, closest
# Beware of the exact policy. Nothing gets downloaded if the wanted resolution is not available
WANTED_RESOLUTION_PREFERENCE = env.str("STRMNTR_RESOLUTION_PREF", 'closest')


# --- Recording resolution runtime setting (V33A) ---
# The recording resolution is a runtime-configurable, persisted setting. Long-
# lived modules (bot.py) must read it via get_recording_resolution() at call time
# so changes made through the settings API take effect without a restart; do not
# `from parameters import WANTED_RESOLUTION` and snapshot the constant.

RECORDING_RESOLUTION_MAX = 'max'
RESOLUTION_PREFERENCES = (
    'closest', 'exact', 'exact_or_least_higher', 'exact_or_highest_lower')
RECORDING_RESOLUTION_STORE = '.strmntr_recording_resolution.json'


def _initial_recording_resolution():
    # STRMNTR_RESOLUTION is only the first-run initial value. When it is unset the
    # default is "max" (always record the highest available source).
    raw = env.str("STRMNTR_RESOLUTION", None)
    if raw is None:
        return RECORDING_RESOLUTION_MAX
    raw = str(raw).strip()
    if raw.lower() == RECORDING_RESOLUTION_MAX:
        return RECORDING_RESOLUTION_MAX
    try:
        return int(raw)
    except (TypeError, ValueError):
        return RECORDING_RESOLUTION_MAX


def _initial_recording_resolution_preference():
    pref = env.str("STRMNTR_RESOLUTION_PREF", 'closest')
    return pref if pref in RESOLUTION_PREFERENCES else 'closest'


_RECORDING_RESOLUTION = _initial_recording_resolution()
_RECORDING_RESOLUTION_PREFERENCE = _initial_recording_resolution_preference()


def get_recording_resolution():
    # Read at call time so runtime changes via the settings API take effect
    # without a restart. Returns "max" or an int height.
    return _RECORDING_RESOLUTION


def get_recording_resolution_preference():
    return _RECORDING_RESOLUTION_PREFERENCE


def set_recording_resolution(resolution, preference=None):
    global _RECORDING_RESOLUTION, _RECORDING_RESOLUTION_PREFERENCE
    _RECORDING_RESOLUTION = resolution
    if preference is not None:
        _RECORDING_RESOLUTION_PREFERENCE = preference


def persist_recording_resolution(resolution, preference):
    # Atomic write-then-rename to a JSON store (same strategy as config.py) so a
    # crash mid-write can't corrupt the setting. The persisted value takes
    # priority over the env var on the next startup (see load_recording_resolution).
    data = {'resolution': resolution, 'preference': preference}
    tmp_path = RECORDING_RESOLUTION_STORE + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, RECORDING_RESOLUTION_STORE)


def load_recording_resolution():
    # Load the persisted store (if any) into the runtime globals, giving it
    # priority over the env-derived initial value. A missing or corrupt file is
    # tolerated and leaves the current values untouched.
    global _RECORDING_RESOLUTION, _RECORDING_RESOLUTION_PREFERENCE
    try:
        with open(RECORDING_RESOLUTION_STORE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return
    if not isinstance(data, dict):
        return
    res = data.get('resolution')
    if res == RECORDING_RESOLUTION_MAX or (isinstance(res, int) and not isinstance(res, bool)):
        _RECORDING_RESOLUTION = res
    pref = data.get('preference')
    if pref in RESOLUTION_PREFERENCES:
        _RECORDING_RESOLUTION_PREFERENCE = pref


load_recording_resolution()

# Specify output container here
# Suggested values are 'mkv' or 'mp4'
CONTAINER = env.str("STRMNTR_CONTAINER", 'mp4')

# Add auto-generated VR format suffix to files
VR_FORMAT_SUFFIX = env.bool("STRMNTR_VR_FORMAT_SUFFIX", True)

# Set ffmpeg readrate to whatever works for you.
# Usually this should be either 0, 1 or 1.3 depending on the network
# Setting it to 0 can result in very fragmented recordings.
# 1 can result in skipped segments
# 1.3 should be the sweet spot but use what works
FFMPEG_READRATE = env.float("STRMNTR_FFMPEG_READRATE", 1.3)

# Specify the segment time in seconds
# If None, the video will be downloaded as a single file
# Example:
# 5 minutes
# SEGMENT_TIME = 300
# 1 hour
# SEGMENT_TIME = 3600
# Also see the ffmpeg documentation for the segment_time option
# You can specify time in hh:mm:ss format
# Example:
# 1 hour
# SEGMENT_TIME = '1:00:00'
SEGMENT_TIME = env.str("STRMNTR_SEGMENT_TIME", None)

# HTTP Manager configuration

# Bind address for the web server
# 0.0.0.0 for remote access from all host
WEBSERVER_HOST = env.str("STRMNTR_HOST", "127.0.0.1")
WEBSERVER_PORT = env.int("STRMNTR_PORT", 5000)

# Web UI skin
# Only the redesigned single-page console (`webui`) is available. The legacy
# skins (kseen715, truck-kun, shaftoverflow) have been removed; any other value
# falls back to `webui`.
WEBSERVER_SKIN = env.str("STRMNTR_SKIN", "webui")

# set frequency in seconds of how often the streamer list will update
WEB_LIST_FREQUENCY = env.int("STRMNTR_LIST_FREQ", 30)

# set frequency in seconds of how often the streamer's status will update on the recording page
WEB_STATUS_FREQUENCY = env.int("STRMNTR_STATUS_FREQ", 5)

# set theater_mode
WEB_THEATER_MODE = env.bool("STRMNTR_THEATER_MODE", False)

# confirm deletes, default to mobile-only.
# set to empty string to disable
# set to "MOBILE" to explicitly confirm deletes only on mobile
# set to any other non-falsy value to always check
WEB_CONFIRM_DELETES = env.str("STRMNTR_CONFIRM_DEL", "MOBILE")

# Password for the web server
# If empty no auth required, else username admin and choosen password
WEBSERVER_PASSWORD = env.str("STRMNTR_PASSWORD", "admin")

# ZMQ (external console) configuration

# The ZMQ interface accepts unauthenticated commands, so it defaults to binding
# only the loopback interface. Set STRMNTR_ZMQ_HOST to 0.0.0.0 for remote access
# from all hosts (not recommended without a firewall) or disable it entirely.
ZMQ_ENABLED = env.bool("STRMNTR_ZMQ_ENABLED", True)
ZMQ_HOST = env.str("STRMNTR_ZMQ_HOST", "127.0.0.1")
ZMQ_PORT = env.int("STRMNTR_ZMQ_PORT", 6969)
