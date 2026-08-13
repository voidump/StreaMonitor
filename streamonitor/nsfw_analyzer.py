"""Local NSFW frame analysis for recordings.

While a stream is being recorded, a background thread grabs one frame from the
live file every parameters.NSFW_SAMPLE_INTERVAL seconds (ffmpeg decodes the
tail of the growing file) and scores it with a local ONNX classifier. When the
recording ends the produced video files are labeled in the media index:
'adult' if any sampled frame scored at or above parameters.NSFW_THRESHOLD,
'non-adult' if every sample scored below it, and 'unknown' when no frame could
be analyzed (missing onnxruntime/numpy, missing model file, ffmpeg failures).
If live sampling produced fewer than parameters.NSFW_MAX_SAMPLES scores and no
adult frame was seen, additional frames are sampled evenly across the finished
file as a fallback.

Everything is off unless parameters.NSFW_ENABLED is True: no thread is
started, no model is loaded and onnxruntime is never imported. All failures
are swallowed so analysis can never affect a recording.

Supported models (parameters.NSFW_MODEL_PATH, bring your own file):
- 2-class normal/nsfw classifiers with NCHW float32 input, e.g. the ViT at
  https://huggingface.co/onnx-community/nsfw_image_detection-ONNX
  (preprocessing: (x/255 - 0.5) / 0.5); adult score = p(nsfw)
- 5-class GantMan/nsfw_model ONNX conversions with NHWC float32 input
  (drawings, hentai, neutral, porn, sexy; preprocessing: x/255);
  adult score = p(hentai) + p(porn) + p(sexy)

Offline entry point (works regardless of the enabled flag):
    python -m streamonitor.nsfw_analyzer <video file> [...]
"""
import os
import subprocess
import threading
import time

import parameters
import streamonitor.log as log

logger = log.Logger("nsfw").get_logger()

ADULT = 'adult'
NON_ADULT = 'non-adult'
UNKNOWN = 'unknown'

_load_lock = threading.Lock()
_infer_lock = threading.Lock()  # single-threaded inference keeps CPU usage bounded
_session = None
_session_failed = False


def _get_session():
    # Lazy-load onnxruntime and the model on first use; a failure (missing
    # package or model file) is logged once and everything degrades to
    # 'unknown' labels from then on.
    global _session, _session_failed
    if _session is not None or _session_failed:
        return _session
    with _load_lock:
        if _session is not None or _session_failed:
            return _session
        try:
            import onnxruntime
            model_path = parameters.NSFW_MODEL_PATH
            if not os.path.isfile(model_path):
                raise FileNotFoundError(f'model file not found: {model_path} '
                                        '(set STRMNTR_NSFW_MODEL_PATH, see streamonitor/nsfw_analyzer.py)')
            opts = onnxruntime.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            _session = onnxruntime.InferenceSession(
                model_path, sess_options=opts, providers=['CPUExecutionProvider'])
        except Exception as e:
            _session_failed = True
            logger.warning(f'NSFW analysis unavailable, videos will be labeled unknown: {e}')
        return _session


def _input_spec(session):
    # Returns (input_name, layout, size) derived from the model's input shape.
    # NCHW ([1, 3, H, W]) or NHWC ([1, H, W, 3]); dynamic dims default to 224.
    inp = session.get_inputs()[0]
    dims = [d if isinstance(d, int) else None for d in inp.shape]
    if len(dims) == 4 and dims[3] == 3:
        return inp.name, 'NHWC', dims[1] or 224
    return inp.name, 'NCHW', (dims[2] if len(dims) == 4 else None) or 224


def _grab_frame(path, size, input_opts=None):
    # Decode one frame straight to raw RGB at the model's input size, so no
    # image library is needed. input_opts are ffmpeg input options (seeking).
    cmd = [parameters.FFMPEG_PATH, '-v', 'error', '-nostdin']
    if input_opts:
        cmd += input_opts
    cmd += ['-i', path, '-frames:v', '1', '-vf', f'scale={size}:{size}',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        return None
    expected = size * size * 3
    if len(proc.stdout) < expected:
        return None
    return proc.stdout[:expected]


def _run_inference(session, buf, layout, size, input_name):
    # Adult probability for one RGB frame, or None if the model layout is not
    # one of the supported 2-class / 5-class layouts.
    import numpy as np
    arr = np.frombuffer(buf, dtype=np.uint8).astype(np.float32).reshape(size, size, 3) / 255.0
    if layout == 'NCHW':
        arr = ((arr - 0.5) / 0.5).transpose(2, 0, 1)
    arr = arr[None, ...]
    with _infer_lock:
        out = session.run(None, {input_name: arr})[0]
    probs = np.asarray(out, dtype=np.float64).reshape(-1)
    if probs.min() < 0 or not 0.99 <= probs.sum() <= 1.01:  # logits -> softmax
        e = np.exp(probs - probs.max())
        probs = e / e.sum()
    if len(probs) == 2:
        return float(probs[1])
    if len(probs) == 5:
        return float(probs[1] + probs[3] + probs[4])
    logger.warning(f'Unsupported model output shape {probs.shape}, labeling unknown')
    return None


def _score_frame(path, input_opts=None):
    # Score one frame of a file; None when the model or the frame is unavailable.
    session = _get_session()
    if session is None:
        return None
    try:
        input_name, layout, size = _input_spec(session)
        buf = _grab_frame(path, size, input_opts)
        if buf is None:
            return None
        return _run_inference(session, buf, layout, size, input_name)
    except Exception as e:
        logger.debug(f'Frame scoring failed for {path}: {e}')
        return None


def _ffprobe_path():
    head, tail = os.path.split(parameters.FFMPEG_PATH)
    if 'ffmpeg' in tail:
        return os.path.join(head, tail.replace('ffmpeg', 'ffprobe'))
    return 'ffprobe'


def _probe_duration(path):
    cmd = [_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'csv=p=0', path]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=30).stdout.decode().strip()
        return float(out)
    except Exception:
        return None


def sample_file(path, count):
    # Score up to `count` frames spaced evenly across a finished file. Falls
    # back to the first frame when the duration cannot be probed.
    if count <= 0:
        return []
    duration = _probe_duration(path)
    if duration and duration > 0:
        opts = [['-ss', f'{duration * (i + 0.5) / count:.3f}'] for i in range(count)]
    else:
        opts = [None]
    scores = []
    for input_opts in opts:
        score = _score_frame(path, input_opts)
        if score is not None:
            scores.append(score)
    return scores


def _label_for(scores, threshold):
    if not scores:
        return UNKNOWN
    return ADULT if max(scores) >= threshold else NON_ADULT


def analyze_file(path, max_samples=None):
    # Offline entry: classify one finished video file. Returns (label, scores).
    scores = sample_file(path, max_samples or parameters.NSFW_MAX_SAMPLES)
    return _label_for(scores, parameters.NSFW_THRESHOLD), scores


class RecordingMonitor:
    """Samples the live recording of one bot and labels the result.

    Created by start_monitor() when a recording starts; the sampling thread
    grabs a frame from the tail of the file being written every
    NSFW_SAMPLE_INTERVAL seconds. finish() stops sampling and finalizes the
    label in a fire-and-forget thread so the bot loop is never blocked.
    """

    def __init__(self, bot):
        self.bot = bot
        self.username = bot.username
        self.folder = bot.outputFolder
        # Base name (no extension) of this recording; the tmp file, VR-suffix
        # and hls _%03d segment outputs all share this prefix.
        self.base = os.path.splitext(bot.recording_filename)[0]
        self.start_ts = time.time()
        self.scores = []
        self.adult_seen = False
        self._grabbed_first_frame = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        interval = max(int(parameters.NSFW_SAMPLE_INTERVAL), 5)
        while not self._stop.wait(interval):
            try:
                self._sample_once()
            except Exception as e:
                logger.debug(f'[{self.username}] live sample failed: {e}')

    def _sample_once(self):
        path = self._live_file()
        if path is None:
            return
        # Seek from the end of the growing file; some containers cannot be
        # tail-seeked while being written (e.g. non-fragmented mp4), so fall
        # back to the first frame once. Short recordings are covered by the
        # post-recording fallback sampling anyway.
        score = _score_frame(path, ['-sseof', '-10'])
        if score is None and not self._grabbed_first_frame:
            self._grabbed_first_frame = True
            score = _score_frame(path)
        if score is not None:
            self.scores.append(score)
            if score >= parameters.NSFW_THRESHOLD:
                self.adult_seen = True

    def _live_file(self):
        # Newest file being written for this recording: tmp files and final
        # outputs share the recording's base prefix, ffmpeg strftime segments
        # share the username prefix and a fresh mtime.
        best = None
        best_mtime = 0
        try:
            for entry in os.scandir(self.folder):
                if not entry.is_file():
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if not (entry.name.startswith(self.base) or
                        (entry.name.startswith(self.username + '-') and st.st_mtime >= self.start_ts - 5)):
                    continue
                if st.st_mtime > best_mtime:
                    best, best_mtime = entry.path, st.st_mtime
        except OSError:
            return None
        return best

    def _final_files(self):
        # Video files produced by this recording (single file, VR suffix or
        # segment variants); tmp files are excluded. Extension check matches
        # Bot.cache_file_list / media_index (case-sensitive).
        from streamonitor.media_index import VIDEO_EXTENSIONS
        finals = []
        try:
            for entry in os.scandir(self.folder):
                if not entry.is_file() or '.tmp.' in entry.name:
                    continue
                if os.path.splitext(entry.name)[1][1:] not in VIDEO_EXTENSIONS:
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if entry.name.startswith(self.base) or \
                        (entry.name.startswith(self.username + '-') and st.st_mtime >= self.start_ts - 5):
                    finals.append(entry.path)
        except OSError:
            pass
        return finals

    def finish(self):
        # Called from the bot loop when the recording ends; must return fast.
        self._stop.set()
        threading.Thread(target=self._finalize, daemon=True).start()

    def _finalize(self):
        try:
            self._thread.join(timeout=30)
        except Exception:
            pass
        try:
            finals = self._final_files()
            if not finals:
                return
            scores = list(self.scores)
            threshold = parameters.NSFW_THRESHOLD
            # Fallback: not enough live samples and nothing adult yet -> sample
            # more frames evenly across the biggest finished file.
            if not self.adult_seen and len(scores) < parameters.NSFW_MAX_SAMPLES:
                largest = max(finals, key=lambda p: os.path.getsize(p) if os.path.isfile(p) else 0)
                scores += sample_file(largest, parameters.NSFW_MAX_SAMPLES - len(scores))
            label = _label_for(scores, threshold)
            from streamonitor.media_index import set_classification
            for path in finals:
                set_classification(path, label)
            logger.info(f'[{self.username}] recording classified {label} '
                        f'({len(scores)} samples, {len(finals)} file(s))')
        except Exception as e:
            logger.warning(f'[{self.username}] NSFW finalization failed: {e}')


def start_monitor(bot):
    # Attach a RecordingMonitor to the bot when the feature is enabled.
    # A no-op (no import, no model load, no thread) when disabled. Never raises.
    try:
        if not parameters.NSFW_ENABLED or not bot.recording_filename:
            return
        monitor = RecordingMonitor(bot)
        monitor.start()
        bot._nsfw_monitor = monitor
    except Exception as e:
        logger.warning(f'Failed to start NSFW monitor: {e}')


def finish_monitor(bot):
    # Detach and finalize the bot's monitor, if any. Never raises.
    try:
        monitor = getattr(bot, '_nsfw_monitor', None)
        bot._nsfw_monitor = None
        if monitor is not None:
            monitor.finish()
    except Exception as e:
        logger.warning(f'Failed to finish NSFW monitor: {e}')


def _main(argv):
    if len(argv) < 2:
        print('usage: python -m streamonitor.nsfw_analyzer <video file> [...]')
        return 2
    if _get_session() is None:
        print('NSFW analysis unavailable (missing onnxruntime/numpy or model file, see log)')
        return 1
    for path in argv[1:]:
        label, scores = analyze_file(path)
        top = f'{max(scores):.3f}' if scores else 'n/a'
        print(f'{path}: {label} (samples={len(scores)}, max adult score={top}, '
              f'threshold={parameters.NSFW_THRESHOLD})')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(_main(sys.argv))
