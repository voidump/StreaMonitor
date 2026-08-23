import errno
import os
import subprocess
import sys
import time

import requests.cookies
from threading import Thread
from parameters import DEBUG, SEGMENT_TIME, CONTAINER, FFMPEG_PATH, FFMPEG_READRATE
from streamonitor.video_filter import remove_undersized_videos

_PROGRESS_POLL_INTERVAL = 2  # seconds between live recording-size samples

# Escalation timeouts for stopping ffmpeg: 'q' on stdin, then SIGTERM, then
# SIGKILL. ffmpeg only reads stdin from its main loop, so a process stuck in a
# blocking network read never sees the 'q' — without the escalation the stop
# path hangs forever and takes process shutdown down with it.
FFMPEG_QUIT_TIMEOUT = 10
FFMPEG_TERM_TIMEOUT = 10
FFMPEG_KILL_TIMEOUT = 5


def getVideoFfmpeg(self, url, filename):
    cmd = [
        FFMPEG_PATH,
        '-user_agent', self.headers['User-Agent']
    ]

    if type(self.cookies) is requests.cookies.RequestsCookieJar:
        cookies_text = ''
        for cookie in self.cookies:
            cookies_text += cookie.name + "=" + cookie.value + "; path=" + cookie.path + '; domain=' + cookie.domain + '\n'
        if len(cookies_text) > 10:
            cookies_text = cookies_text[:-1]
        cmd.extend([
            '-cookies', cookies_text
        ])

    if FFMPEG_READRATE:
        cmd.extend(['-readrate', f'{FFMPEG_READRATE!s}'])

    cmd.extend([
        '-max_reload', '20',
        '-seg_max_retry', '20',
        '-m3u8_hold_counters', '20',
        '-i', url,
        '-c:a', 'copy',
        '-c:v', 'copy',
    ])

    suffix = ''
    if hasattr(self, 'filename_extra_suffix'):
        suffix = self.filename_extra_suffix

    if SEGMENT_TIME is not None:
        username = filename.rsplit('-', maxsplit=2)[0]
        cmd.extend([
            '-f', 'segment',
            '-reset_timestamps', '1',
            '-segment_time', str(SEGMENT_TIME),
            '-strftime', '1',
            f'{username}-%Y%m%d-%H%M%S{suffix}.{CONTAINER}'
        ])
    else:
        cmd.extend([
            os.path.splitext(filename)[0] + suffix + '.' + CONTAINER
        ])

    class _Stopper:
        def __init__(self):
            self.stop = False

        def pls_stop(self):
            self.stop = True

    stopping = _Stopper()
    error = False

    def execute():
        nonlocal error
        stderr = open(filename + '.stderr.log', 'w+') if DEBUG else subprocess.DEVNULL
        try:
            try:
                startupinfo = None
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                process = subprocess.Popen(
                    args=cmd, stdin=subprocess.PIPE, stderr=stderr, stdout=subprocess.DEVNULL, startupinfo=startupinfo)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    self.logger.error('FFMpeg executable not found!')
                    error = True
                    return
                else:
                    self.logger.error("Got OSError, errno: " + str(e.errno))
                    error = True
                    return

            while process.poll() is None:
                if stopping.stop:
                    try:
                        process.communicate(b'q', timeout=FFMPEG_QUIT_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        self.logger.warning('ffmpeg ignored quit, terminating')
                        process.terminate()
                        try:
                            process.wait(FFMPEG_TERM_TIMEOUT)
                        except subprocess.TimeoutExpired:
                            self.logger.error('ffmpeg did not terminate, killing')
                            process.kill()
                            try:
                                process.wait(FFMPEG_KILL_TIMEOUT)
                            except subprocess.TimeoutExpired:
                                pass
                    except (OSError, ValueError):
                        pass  # process already exited / pipes closed
                    break
                try:
                    process.wait(1)
                except subprocess.TimeoutExpired:
                    pass

            if process.returncode and process.returncode != 0 and process.returncode != 255:
                self.logger.error('The process exited with an error. Return code: ' + str(process.returncode))
                error = True
                return
        finally:
            if hasattr(stderr, "close"):
                stderr.close()

    # Arm the stop callback before the worker starts: a stop() arriving in the
    # gap between Popen and the assignment used to be silently lost, leaving
    # the recording running until the stream ended on its own.
    self.stopDownload = lambda: stopping.pls_stop()
    thread = Thread(target=execute)
    thread.start()

    # Poll the output file(s) on disk so the list/card UI can show a live
    # recording speed. ffmpeg writes the container directly (single file, or
    # timestamped segments sharing the username prefix), so summing the sizes of
    # freshly-written matching files tracks the growth without parsing progress.
    folder = os.path.dirname(filename)
    username_prefix = os.path.basename(filename).rsplit('-', maxsplit=2)[0]
    start_ts = time.time() - _PROGRESS_POLL_INTERVAL

    def current_size():
        total = 0
        try:
            for entry in os.scandir(folder):
                if not entry.is_file():
                    continue
                if not entry.name.startswith(username_prefix):
                    continue
                if not entry.name.endswith('.' + CONTAINER):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_mtime >= start_ts:
                    total += st.st_size
        except OSError:
            return None
        return total

    def monitor():
        while thread.is_alive():
            size = current_size()
            if size is not None:
                self.update_recording_progress(size)
            time.sleep(_PROGRESS_POLL_INTERVAL)

    monitor_thread = Thread(target=monitor)
    monitor_thread.start()
    thread.join()
    monitor_thread.join()
    self.stopDownload = None
    if error:
        return False

    # ffmpeg writes the final container(s) directly. Filter out undersized
    # outputs from this session: the single output path, or the timestamped
    # segments sharing the username prefix that were written after start.
    if SEGMENT_TIME is not None:
        produced = []
        try:
            for entry in os.scandir(folder):
                if not entry.is_file():
                    continue
                if not entry.name.startswith(username_prefix):
                    continue
                if not entry.name.endswith('.' + CONTAINER):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_mtime >= start_ts:
                    produced.append(entry.path)
        except OSError:
            produced = []
    else:
        produced = [os.path.splitext(filename)[0] + suffix + '.' + CONTAINER]

    remove_undersized_videos(produced, self.logger)
    return True
