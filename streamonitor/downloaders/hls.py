import m3u8
import os
import subprocess
from threading import Thread
from ffmpy import FFmpeg, FFRuntimeError
from time import sleep
from parameters import DEBUG, CONTAINER, SEGMENT_TIME, FFMPEG_PATH
from streamonitor.http_session import DEFAULT_TIMEOUT

_http_lib = None
if not _http_lib:
    try:
        import pycurl_requests as requests
        _http_lib = 'pycurl'
    except ImportError:
        pass
if not _http_lib:
    try:
        import requests
        _http_lib = 'requests'
    except ImportError:
        pass
if not _http_lib:
    raise ImportError("Please install requests or pycurl package to proceed")


def getVideoNativeHLS(self, url, filename, m3u_processor=None):
    self.stopDownloadFlag = False
    error = False
    tmpfilename = filename[:-len('.' + CONTAINER)] + '.tmp.ts'
    session = requests.Session()

    def execute():
        nonlocal error
        downloaded_list = []
        consecutive_failures = 0
        max_consecutive_failures = 15  # Tolerate transient 404s from LL-HLS
        total_bytes = 0
        segment_count = 0
        self.recording_bytes = 0
        self.recording_segments = 0
        try:
            with open(tmpfilename, 'wb') as outfile:
                self.logger.info(f'HLS recording to: {os.path.basename(tmpfilename)}')
                while not self.stopDownloadFlag:
                    did_download = False
                    try:
                        r = session.get(url, headers=self.headers, cookies=self.cookies, timeout=DEFAULT_TIMEOUT)
                        content = r.content.decode("utf-8")
                    except Exception as e:
                        self.logger.warning(f'Failed to fetch playlist: {e}')
                        consecutive_failures += 1
                        if consecutive_failures >= max_consecutive_failures:
                            self.logger.error(f'Stopping: {consecutive_failures} consecutive playlist failures')
                            return
                        sleep(2)
                        continue
                    if m3u_processor:
                        content = m3u_processor(content)
                    # Skip ADVERT playlists (promotional/ad content)
                    if '#EXT-X-MOUFLON-ADVERT' in content:
                        self.logger.warning('Stopping: received ADVERT playlist')
                        return
                    chunklist = m3u8.loads(content)
                    if len(chunklist.segments) == 0:
                        if total_bytes > 0:
                            self.logger.info(f'Stream ended: {total_bytes/1024/1024:.1f} MB, {segment_count} segments')
                        else:
                            self.logger.warning('Stopping: playlist returned 0 segments')
                        return
                    for chunk in chunklist.segment_map + chunklist.segments:
                        if chunk.uri in downloaded_list:
                            continue
                        downloaded_list.append(chunk.uri)
                        chunk_uri = chunk.uri
                        self.debug('Downloading ' + chunk_uri)
                        if not chunk_uri.startswith("https://"):
                            chunk_uri = '/'.join(url.split('.m3u8')[0].split('/')[:-1]) + '/' + chunk_uri
                        try:
                            m = session.get(chunk_uri, headers=self.headers, cookies=self.cookies, timeout=DEFAULT_TIMEOUT)
                        except Exception as e:
                            self.logger.warning(f'Segment download error: {e}')
                            consecutive_failures += 1
                            if consecutive_failures >= max_consecutive_failures:
                                self.logger.error(f'Stopping: {consecutive_failures} consecutive segment errors')
                                return
                            continue
                        if m.status_code != 200:
                            # LL-HLS segments expire fast, 404 is common - skip and continue
                            self.logger.warning(f'Segment HTTP {m.status_code} (fail #{consecutive_failures+1}), skipping')
                            consecutive_failures += 1
                            if consecutive_failures >= max_consecutive_failures:
                                self.logger.error(f'Stopping: {consecutive_failures} consecutive segment failures')
                                return
                            continue
                        # Successful download - reset failure counter
                        consecutive_failures = 0
                        did_download = True
                        seg_bytes = len(m.content)
                        outfile.write(m.content)
                        outfile.flush()
                        total_bytes += seg_bytes
                        segment_count += 1
                        self.recording_bytes = total_bytes
                        self.recording_segments = segment_count
                        # Progress log every 30 segments (~1 minute)
                        if segment_count % 30 == 0:
                            self.logger.info(f'Recording progress: {total_bytes/1024/1024:.1f} MB, {segment_count} segments')
                        if self.stopDownloadFlag:
                            return
                    if not did_download:
                        sleep(2)
            if total_bytes > 0:
                self.logger.info(f'Recording complete: {total_bytes/1024/1024:.1f} MB, {segment_count} segments')
        except Exception as e:
            self.logger.error(f'HLS download error: {e}')
            error = True
        finally:
            self.recording_bytes = 0
            self.recording_segments = 0

    def terminate():
        self.stopDownloadFlag = True

    process = Thread(target=execute)
    process.start()
    self.stopDownload = terminate
    process.join()
    self.stopDownload = None

    if error:
        return False

    if not os.path.exists(tmpfilename):
        return False

    if os.path.getsize(tmpfilename) == 0:
        os.remove(tmpfilename)
        return False

    # Post-processing
    try:
        stdout = open(filename + '.postprocess_stdout.log', 'w+') if DEBUG else subprocess.DEVNULL
        stderr = open(filename + '.postprocess_stderr.log', 'w+') if DEBUG else subprocess.DEVNULL
        output_str = '-c:a copy -c:v copy'
        suffix = ''
        if SEGMENT_TIME is not None:
            output_str += f' -f segment -reset_timestamps 1 -segment_time {str(SEGMENT_TIME)}'
            if hasattr(self, 'filename_extra_suffix'):
                suffix = self.filename_extra_suffix
            filename = filename[:-len('.' + CONTAINER)] + '_%03d' + suffix + '.' + CONTAINER
        ff = FFmpeg(executable=FFMPEG_PATH, inputs={tmpfilename: None}, outputs={filename: output_str})
        ff.run(stdout=stdout, stderr=stderr)
        os.remove(tmpfilename)
    except FFRuntimeError as e:
        if e.exit_code and e.exit_code != 255:
            return False

    return True
