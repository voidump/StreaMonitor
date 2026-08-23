import os
import subprocess
import sys


def test_import_succeeds_with_resolution_max(tmp_path):
    # STRMNTR_RESOLUTION=max is the documented default of the runtime
    # resolution setting; a leftover env.int constant used to crash the whole
    # application at import time when it was set.
    env = dict(os.environ, STRMNTR_RESOLUTION='max')
    result = subprocess.run(
        [sys.executable, '-c', 'import parameters'],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
