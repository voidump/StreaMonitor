import glob
import json
import os
import subprocess
from threading import Thread
from time import sleep
from websocket import create_connection, WebSocketConnectionClosedException, WebSocketException
from contextlib import closing
from ffmpy import FFmpeg, FFRuntimeError
from parameters import DEBUG, CONTAINER, SEGMENT_TIME, FFMPEG_PATH
from streamonitor.video_filter import remove_undersized_videos


def getVideoWSSVR(self, url, filename):
    self.stopDownloadFlag = False
    error = False
    url = url.replace('fmp4s://', 'wss://')

    suffix = ''
    if hasattr(self, 'filename_extra_suffix'):
        suffix = self.filename_extra_suffix

    basefilename = filename[:-len('.' + CONTAINER)]
    filename = basefilename + suffix + '.' + CONTAINER
    tmpfilename = basefilename + '.tmp.mp4'

    def debug_(message):
        self.debug(message, filename + '.log')

    def execute():
        nonlocal error
        consecutive_failures = 0
        max_consecutive_failures = 15
        total_bytes = 0
        with open(tmpfilename, 'wb') as outfile:
            while not self.stopDownloadFlag:
                try:
                    with closing(create_connection(url, timeout=10)) as conn:
                        conn.send('{"url":"stream/hello","version":"0.0.1"}')
                        while not self.stopDownloadFlag:
                            t = conn.recv()
                            try:
                                tj = json.loads(t)
                                if 'url' in tj:
                                    if tj['url'] == 'stream/qual':
                                        conn.send('{"quality":"test","url":"stream/play","version":"0.0.1"}')
                                        debug_('Connection opened')
                                        consecutive_failures = 0
                                        break
                                if 'message' in tj:
                                    if tj['message'] == 'ping':
                                        debug_('Server is not ready or there was a change')
                                        error = True
                                        return
                            except Exception:
                                debug_('Failed to open the connection')
                                error = True
                                return

                        while not self.stopDownloadFlag:
                            chunk = conn.recv()
                            outfile.write(chunk)
                            if isinstance(chunk, (bytes, bytearray)):
                                total_bytes += len(chunk)
                                # Live size for the list/card UI, same as the
                                # other downloaders; without it VR recordings
                                # showed 0 B/s for their whole duration.
                                self.update_recording_progress(total_bytes)
                except WebSocketConnectionClosedException:
                    debug_('WebSocket connection closed - try to continue')
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        self.logger.warning(
                            f'Stopping: {consecutive_failures} consecutive reconnects')
                        return
                    sleep(2)  # backoff: an instantly-closing server was hammered in a tight loop
                    continue
                except (WebSocketException, OSError) as wex:
                    # OSError covers DNS failures / ECONNREFUSED from
                    # create_connection, which used to escape and kill the
                    # thread with error still False (the empty tmp then went
                    # through remux as if the recording had succeeded).
                    debug_('Error when downloading')
                    debug_(wex)
                    self.logger.warning(f'WSS download error: {wex}')
                    error = True
                    return

    def terminate():
        self.stopDownloadFlag = True

    process = Thread(target=execute)
    process.start()
    self.stopDownload = terminate
    process.join()
    self.stopDownload = None

    if error:
        return False

    if getattr(self, 'quitting', False) and os.path.exists(tmpfilename) \
            and os.path.getsize(tmpfilename) > 0:
        # Same as hls.py: don't let a size-proportional remux outlive the
        # shutdown deadline; the startup repair chain finishes the job.
        self.logger.info('Shutdown in progress; leaving temp file for startup repair')
        return True

    # Post-processing
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    try:
        if DEBUG:
            stdout = open(filename + '.postprocess_stdout.log', 'w+')
            stderr = open(filename + '.postprocess_stderr.log', 'w+')
        output_str = '-c:a copy -c:v copy'
        if SEGMENT_TIME is not None:
            output_str += f' -f segment -reset_timestamps 1 -segment_time {str(SEGMENT_TIME)}'
            filename = basefilename + '_%03d' + suffix + '.' + CONTAINER
        ff = FFmpeg(executable=FFMPEG_PATH, inputs={tmpfilename: '-ignore_editlist 1'}, outputs={filename: output_str})
        ff.run(stdout=stdout, stderr=stderr)
    except FFRuntimeError as e:
        if e.exit_code and e.exit_code != 255:
            # Remux failed: keep the temp file for later repair.
            return False
    finally:
        if hasattr(stdout, "close"):
            stdout.close()
        if hasattr(stderr, "close"):
            stderr.close()

    # Only delete the temp file once the remuxed output exists and is non-empty.
    if SEGMENT_TIME is not None:
        produced = glob.glob(filename.replace('%03d', '*'))
        output_ok = any(os.path.exists(f) and os.path.getsize(f) > 0 for f in produced)
    else:
        produced = [filename]
        output_ok = os.path.exists(filename) and os.path.getsize(filename) > 0

    if not output_ok:
        return False

    os.remove(tmpfilename)
    remove_undersized_videos(produced, self.logger)
    return True
