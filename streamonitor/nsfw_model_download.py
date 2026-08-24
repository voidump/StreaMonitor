"""One-click download of a curated NSFW classifier model.

One session runs at a time in a single background thread (same start/progress/
cancel shape as nsfw_backfill): the file is streamed to "<name>.part" next to
its final location, validated (size, and loadability when onnxruntime is
installed) and only then renamed into place, so a cancelled or failed download
never leaves a usable-looking model behind. All failures degrade to an error
string in the progress snapshot; nothing here can affect recordings.

The catalog only lists ONNX files nsfw_analyzer.py can actually run (2-class
normal/nsfw ViT, NCHW float32 input). Sizes are the HuggingFace blob sizes and
are treated as a hint: the real expectation is the Content-Length of the
response.
"""
import os
import shutil
import threading
import time

import parameters
import streamonitor.log as log

logger = log.Logger("nsfw_model_download").get_logger()

CATALOG = (
    {
        'id': 'vit-nsfw-quantized',
        'name': 'NSFW image detection (ViT, quantized)',
        'desc': 'Smaller and faster, slightly less accurate. Recommended default.',
        'url': 'https://huggingface.co/onnx-community/nsfw_image_detection-ONNX'
               '/resolve/main/onnx/model_quantized.onnx',
        'filename': 'nsfw-model-quantized.onnx',
        'sizeBytes': 87333629,
    },
    {
        'id': 'vit-nsfw',
        'name': 'NSFW image detection (ViT, full precision)',
        'desc': 'Full precision weights, most accurate but ~4x the download.',
        'url': 'https://huggingface.co/onnx-community/nsfw_image_detection-ONNX'
               '/resolve/main/onnx/model.onnx',
        'filename': 'nsfw-model.onnx',
        'sizeBytes': 343401688,
    },
)

# Free space required before starting: the expected size plus 20% headroom.
_SPACE_MARGIN = 1.2
_CHUNK_SIZE = 1 << 20
_MIN_MODEL_BYTES = 1 << 20   # anything smaller is an error page, not a model

_lock = threading.Lock()
_thread = None
_cancel = threading.Event()
_state = {
    'running': False,
    'catalogId': None,
    'filename': None,
    'totalBytes': 0,
    'doneBytes': 0,
    'error': None,
    'startedTs': None,
}


class _Cancelled(Exception):
    pass


def catalog():
    return [dict(entry) for entry in CATALOG]


def _find(catalog_id):
    return next((entry for entry in CATALOG if entry['id'] == catalog_id), None)


def model_dir():
    # The model directory (directory of the active model file), resolved at
    # call time so a model switch is picked up.
    getter = getattr(parameters, 'get_nsfw_models_dir', None)
    if callable(getter):
        return getter()
    return os.path.dirname(os.path.abspath(os.path.expanduser(parameters.NSFW_MODEL_PATH)))


def progress():
    with _lock:
        return dict(_state)


def _check_loadable(path):
    # Refuse a file onnxruntime cannot open (a truncated or wrong-content
    # download). Skipped when the runtime is not installed.
    try:
        import onnxruntime
    except Exception:
        return
    opts = onnxruntime.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    onnxruntime.InferenceSession(path, sess_options=opts,
                                 providers=['CPUExecutionProvider'])


def _validate(path, expected_total):
    size = os.path.getsize(path)
    if size < _MIN_MODEL_BYTES or (expected_total and size != expected_total):
        raise ValueError(f'incomplete download ({size} bytes)')
    _check_loadable(path)


def _run(entry, dest):
    part = dest + '.part'
    try:
        import requests
        with requests.get(entry['url'], stream=True, timeout=(10, 60),
                          headers={'User-Agent': parameters.HTTP_USER_AGENT}) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get('Content-Length') or 0) or entry['sizeBytes']
            with _lock:
                _state['totalBytes'] = total
            with open(part, 'wb') as f:
                for chunk in resp.iter_content(_CHUNK_SIZE):
                    if _cancel.is_set():
                        raise _Cancelled()
                    if not chunk:
                        continue
                    f.write(chunk)
                    with _lock:
                        _state['doneBytes'] += len(chunk)
        _validate(part, total)
        os.replace(part, dest)
        logger.info(f"NSFW model downloaded: {dest}")
    except _Cancelled:
        _remove(part)
        logger.info(f"NSFW model download cancelled: {entry['id']}")
    except Exception as e:
        _remove(part)
        logger.warning(f"NSFW model download failed for {entry['id']}: {e}")
        with _lock:
            _state['error'] = str(e)
    finally:
        with _lock:
            _state['running'] = False


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def start(catalog_id):
    # Returns (progress, started, error). error is (code, message): unknown_model
    # for an id outside the catalog, exists when the target file is already
    # there, insufficient_space when the expected size does not fit. A download
    # that is already running is never restarted, its state is returned instead.
    global _thread
    entry = _find(catalog_id)
    if entry is None:
        return progress(), False, ('unknown_model', 'Unknown model id')
    with _lock:
        if _state['running']:
            return dict(_state), False, None
    directory = model_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as e:
        return progress(), False, ('create_failed', f'Cannot create model directory: {e}')
    dest = os.path.join(directory, entry['filename'])
    if os.path.exists(dest):
        return progress(), False, ('exists', 'Model file already exists')
    try:
        free = shutil.disk_usage(directory).free
    except Exception:
        free = None
    if free is not None and free < entry['sizeBytes'] * _SPACE_MARGIN:
        return progress(), False, ('insufficient_space',
                                   'Not enough free disk space for this model')
    with _lock:
        if _state['running']:
            return dict(_state), False, None
        _cancel.clear()
        _state.update({'running': True, 'catalogId': entry['id'],
                       'filename': entry['filename'], 'totalBytes': entry['sizeBytes'],
                       'doneBytes': 0, 'error': None, 'startedTs': int(time.time())})
    _thread = threading.Thread(target=_run, args=(entry, dest), daemon=True)
    _thread.start()
    return progress(), True, None


def cancel():
    # Stops after the chunk being written finishes and removes the .part file;
    # a no-op when idle.
    _cancel.set()
    return progress()
