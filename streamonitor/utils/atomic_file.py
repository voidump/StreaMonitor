import json
import os
import tempfile

# Snapshot the process umask once (import happens single-threaded); os.umask
# can only be read by setting it, which is not thread-safe later on.
_UMASK = os.umask(0)
os.umask(_UMASK)


def atomic_write_text(path, text, mode=None):
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
        # mkstemp creates 0600; without an explicit chmod the rename would
        # silently tighten world-readable targets. The default honors the
        # process umask exactly like a plain open() would.
        os.chmod(tmp_path, mode if mode is not None else (0o666 & ~_UMASK))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path, data, indent=4, mode=None):
    atomic_write_text(path, json.dumps(data, indent=indent), mode=mode)
