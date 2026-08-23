import json
import os
import tempfile


def atomic_write_text(path, text, mode=0o644):
    # Unique temp file in the target's directory + flush + fsync + os.replace:
    # concurrent writers cannot truncate each other's half-written temp or race
    # on a shared rename (the old fixed "<path>.tmp" scheme allowed both), and
    # a crash or power loss mid-write leaves the previous file intact.
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; apply the intended mode explicitly so the
        # rename doesn't silently change the target's permissions.
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path, data, indent=4, mode=0o644):
    atomic_write_text(path, json.dumps(data, indent=indent), mode=mode)
