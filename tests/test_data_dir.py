import os
import subprocess
import sys

_PROBE = '''
import parameters
import streamonitor.config as config
import streamonitor.online_history as online_history
from streamonitor.managers.httpmanager.httpmanager import SESSION_SECRET_FILE
from streamonitor.sites.stripchat import StripChat
print(parameters.data_path(".env"))
print(parameters.RECORDING_RESOLUTION_STORE)
print(parameters.NSFW_MODEL_PATH)
print(config.config_loc)
print(config.history_loc)
print(SESSION_SECRET_FILE)
print(online_history._store_path())
print(StripChat._mouflon_cache_filename)
'''


def _probe(extra_env):
    result = subprocess.run(
        [sys.executable, '-c', _PROBE],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, **extra_env}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()


def test_all_state_files_follow_data_dir(tmp_path):
    lines = _probe({'STRMNTR_DATA_DIR': str(tmp_path)})
    for line in lines:
        assert line.startswith(str(tmp_path)), f'{line} escaped the data dir'


def test_default_layout_is_unchanged(tmp_path):
    # Without STRMNTR_DATA_DIR everything stays a plain working-directory
    # relative name, exactly as before the data-dir feature.
    env = {k: v for k, v in os.environ.items() if k != 'STRMNTR_DATA_DIR'}
    result = subprocess.run(
        [sys.executable, '-c', _PROBE],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == '.env'
    assert lines[3] == 'config.json'
    assert lines[4] == 'history.json'
    assert lines[5] == '.session_secret'
