from __future__ import unicode_literals
import os
import time
import traceback
from enum import Enum
from urllib.parse import urljoin

import m3u8
from time import sleep
from datetime import datetime
from threading import Thread

import requests
import requests.cookies

from streamonitor.enums import Status, COUNTRIES, Gender, GENDER_DATA
from streamonitor.http_session import TimeoutSession
import streamonitor.log as log
import streamonitor.online_history as online_history
import parameters
from parameters import DEBUG, CONTAINER, HTTP_USER_AGENT
from streamonitor.downloaders.ffmpeg import getVideoFfmpeg
from streamonitor.media_index import record_finished_file
from streamonitor.models import VideoData
from streamonitor import nsfw_analyzer

LOADED_SITES = set()

# Rolling window (seconds) used to derive live recording speed from byte samples.
RECORDING_SPEED_WINDOW = 15


class Bot(Thread):
    site = None
    siteslug = None
    aliases = []
    ratelimit = False
    bulk_update = False

    sleep_on_private = 5
    sleep_on_offline = 5
    sleep_on_long_offline = 300
    sleep_on_error = 20
    sleep_on_ratelimit = 180
    long_offline_timeout = 600
    # Minimum seconds between single getStatus() fallbacks while a bulk-update
    # streamer is stranded in UNKNOWN. The bulk loop sleeps 1s, so without this
    # a wedged UNKNOWN streamer would re-check roughly once per second.
    min_unknown_fallback_interval = 20

    headers = {
        "User-Agent": HTTP_USER_AGENT
    }

    status_messages = {
        Status.UNKNOWN: "Unknown error",
        Status.PUBLIC: "Channel online",
        Status.OFFLINE: "No stream",
        Status.LONG_OFFLINE: "No stream for a while",
        Status.PRIVATE: "Private show",
        Status.RATELIMIT: "Rate limited",
        Status.NOTEXIST: "Nonexistent user",
        Status.NOTRUNNING: "Not running",
        Status.ERROR: "Error on downloading",
        Status.RESTRICTED: "Model is restricted, maybe geo-block"
    }

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        if cls.site:
            global LOADED_SITES
            LOADED_SITES.add(cls)

    def __init__(self, username):
        super().__init__()
        # Daemon: orderly shutdown is CleanExit's explicit wait; a stuck or
        # leaked bot thread must never hang interpreter exit. Also keeps the
        # daemon flag consistent whether the bot is created by the main thread
        # (startup) or a web request thread (which is itself a daemon).
        self.daemon = True
        self.username = username
        self.logger = self.getLogger()

        self.session = TimeoutSession()
        self.session.headers.update(self.headers)
        self.cookies = None
        self.cookieUpdater = None
        self.cookie_update_interval = 0

        self.lastInfo = {}  # This dict will hold information about stream after getStatus is called. One can use this in getVideoUrl
        self.running = False
        self.quitting = False
        self.sc: Status = Status.NOTRUNNING  # Status code
        self.previous_status = None
        self.getVideo = getVideoFfmpeg
        self.stopDownload = None
        self.recording = False
        self.recording_filename = None  # Basename of the file currently being written
        self.recording_bytes = 0  # Live recording size in bytes
        self.recording_segments = 0  # Live recording segment count
        self.recording_resolution = None  # Short side (px) of the selected stream
        self.recording_speed_bps = None  # Live recording speed in bytes/sec
        self._recording_speed_samples = []  # (timestamp, bytes) rolling samples
        self.lastCheckTs = None  # Epoch timestamp of last status check
        self._last_unknown_fallback_ts = None  # Last single getStatus() fallback while UNKNOWN
        self.monitorState = 'active'  # active | paused | stopped (v3 web UI)
        self.video_files = []
        self.video_files_total_size = 0
        self.cache_file_list()

        self.gender = None
        self.country = None
        self.url = self.getWebsiteURL()

    def setUsername(self, username):
        self.username = username
        self.logger = self.getLogger()
        self.cache_file_list()
        self.url = self.getWebsiteURL()

    def getLogger(self):
        if hasattr(self, 'logger') and self.logger:
            self.logger.removeHandler(self.logger.handlers[0])
        return log.Logger("[" + self.siteslug + "] " + self.username).get_logger()

    def restart(self):
        self.running = True

    def stop(self, a, b, thread_too=False):
        if self.running:
            self.log("Stopping...")
            if self.stopDownload:
                self.stopDownload()
            self.running = False
        if thread_too:
            self.quitting = True

    def getStatus(self):
        return Status.UNKNOWN

    def log(self, message):
        self.logger.info(message)

    def debug(self, message, filename=None):
        if DEBUG:
            self.logger.debug(message)
            if not filename:
                filename = os.path.join(self.outputFolder, 'debug.log')
            with open(filename, 'a+') as debugfile:
                debugfile.write(message + '\n')

    def status(self):
        message = self.status_messages.get(self.sc) or self.status_messages.get(Status.UNKNOWN)
        if self.sc == Status.NOTEXIST:
            self.running = False
        return message

    def getWebsiteURL(self):
        return "javascript:void(0)"

    @property
    def country_data(self):
        return COUNTRIES.get(self.country, {'flag': '', 'name': 'Unknown'})

    @property
    def gender_data(self):
        return GENDER_DATA.get(self.gender, GENDER_DATA.get(Gender.UNKNOWN))

    def cache_file_list(self):
        # Relocated non-adult videos live under "<nonadult dir>/<same folder
        # name>" (see media_index.relocate_non_adult) and recycled ones under
        # the recycle bin (see media_index.apply_nsfw_action); include both so
        # the web UI keeps listing, playing, thumbnailing and deleting moved
        # files until the retention purge removes them.
        folders = [self.outputFolder, self.nonAdultFolder, self.recycleFolder]
        _videos = []
        _total_size = 0
        for videos_folder in folders:
            if not os.path.isdir(videos_folder):
                continue
            try:
                for file in os.scandir(videos_folder):
                    if file.is_dir():
                        continue
                    if not os.path.splitext(file.name)[1][1:] in ['mp4', 'mkv', 'webm', 'mov', 'avi', 'wmv']:
                        continue
                    video = VideoData(file, self.username)
                    _total_size += video.filesize
                    _videos.append(video)
            except Exception as e:
                self.logger.warning(e)
        self.video_files = _videos
        self.video_files_total_size = _total_size

    def update_recording_progress(self, total_bytes):
        # Record the live size and derive a rolling speed over RECORDING_SPEED_WINDOW.
        # Shared by every downloader so speed works uniformly across sites.
        self.recording_bytes = total_bytes
        now = time.time()
        samples = self._recording_speed_samples
        samples.append((now, total_bytes))
        cutoff = now - RECORDING_SPEED_WINDOW
        while len(samples) > 2 and samples[0][0] < cutoff:
            samples.pop(0)
        if len(samples) >= 2:
            dt = samples[-1][0] - samples[0][0]
            db = samples[-1][1] - samples[0][1]
            if dt > 0 and db >= 0:
                self.recording_speed_bps = db / dt

    def reset_recording_progress(self):
        # Clear all live recording metrics once a recording ends so the next one
        # starts clean and the UI degrades gracefully to no values.
        self.recording_bytes = 0
        self.recording_segments = 0
        self.recording_resolution = None
        self.recording_speed_bps = None
        self._recording_speed_samples = []

    def _probe_finished_recording(self, finished_name):
        # Fire-and-forget: warm the duration cache for the just-finished recording
        # so the web detail view never has to probe it on demand. Never raises and
        # never affects the recording loop; a no-op when the web API is unavailable.
        try:
            from streamonitor.managers.httpmanager.api_v2 import enqueue_duration_probe
        except Exception:
            return
        try:
            target = None
            for video in self.video_files:
                if finished_name and video.filename == finished_name:
                    target = video
                    break
            if target is None and self.video_files:
                target = max(self.video_files, key=lambda v: v._stat.st_mtime)
            if target is not None:
                enqueue_duration_probe(target.abs_path, int(target._stat.st_mtime))
        except Exception:
            pass

    def _sleep(self, time):
        while time > 0:
            sleep(1)
            time -= 1
            if self.quitting or not self.running:
                return

    def _should_do_single_status_check(self, now):
        # Non-bulk sites always poll on their own cadence (governed by the
        # sleep_on_* delays at the end of the loop).
        if not self.bulk_update:
            return True
        # Bulk-update sites normally rely on the manager's batch call; they only
        # fall back to a single getStatus() when a streamer is stranded.
        if self.sc == Status.NOTRUNNING:
            return True
        if self.sc == Status.UNKNOWN:
            last = self._last_unknown_fallback_ts
            return last is None or (now - last) >= self.min_unknown_fallback_interval
        return False

    def run(self):
        while not self.quitting:
            while not self.running and not self.quitting:
                sleep(1)
            if self.quitting:
                break

            offline_time = self.long_offline_timeout + 1  # Don't start polling when streamer was offline at start
            while self.running:
                try:
                    self.recording = False
                    self.recording_filename = None
                    now = time.time()
                    if self._should_do_single_status_check(now):
                        try:
                            self.sc = self.getStatus()
                        except Exception as e:
                            self.logger.exception(e)
                            self.sc = Status.ERROR
                        self.lastCheckTs = datetime.now().timestamp()
                        # Throttle repeated fallbacks only while stranded in
                        # UNKNOWN; any other outcome clears the backoff so a
                        # fresh UNKNOWN is retried immediately.
                        self._last_unknown_fallback_ts = now if self.sc == Status.UNKNOWN else None
                    # Check if the status has changed and log the update if it's different from the previous status
                    if self.sc != self.previous_status:
                        self.log(self.status())
                        self.previous_status = self.sc
                    # Record an "online" observation for the schedule/heatmap. Hour
                    # granularity with in-memory per-hour dedup keeps this off the
                    # disk on all but the first poll of each hour; never fatal.
                    if self.sc in (Status.PUBLIC, Status.PRIVATE):
                        try:
                            online_history.record_online(self.site, self.username)
                        except Exception:
                            pass
                    if self.sc == Status.ERROR:
                        self._sleep(self.sleep_on_error)
                    if self.sc == Status.OFFLINE:
                        offline_time += self.sleep_on_offline
                        if offline_time > self.long_offline_timeout:
                            self.sc = Status.LONG_OFFLINE
                    elif self.sc == Status.PUBLIC or self.sc == Status.PRIVATE:
                        offline_time = 0
                        if self.sc == Status.PUBLIC:
                            if self.cookie_update_interval > 0 and self.cookieUpdater is not None:
                                def update_cookie():
                                    while self.sc == Status.PUBLIC and not self.quitting and self.running:
                                        self._sleep(self.cookie_update_interval)
                                        ret2 = self.cookieUpdater()
                                        if ret2:
                                            self.debug('Updated cookies')
                                        else:
                                            self.logger.warning('Failed to update cookies')
                                cookie_update_process = Thread(target=update_cookie)
                                cookie_update_process.start()

                            try:
                                video_url = self.getVideoUrl()
                            except Exception as e:
                                self.logger.exception(e)
                                self.logger.error('Failed to get video url')
                                video_url = None
                            if video_url is None:
                                self.sc = Status.ERROR
                                self.logger.error(self.status())
                                self._sleep(self.sleep_on_error)
                                continue
                            self.log('Started downloading show')
                            self.recording = True
                            file = self.genOutFilename()
                            self.recording_filename = os.path.basename(file)
                            nsfw_analyzer.start_monitor(self)
                            try:
                                ret = self.getVideo(self, video_url, file)
                            except Exception as e:
                                self.logger.exception(e)
                                ret = False
                            nsfw_analyzer.finish_monitor(self)
                            if not ret:
                                self.reset_recording_progress()
                                self.log('Recording ended with error')
                                self.sc = Status.ERROR
                                self.log(self.status())
                                self._sleep(self.sleep_on_error)
                                continue
                            finished_name = self.recording_filename
                            self.recording = False
                            self.recording_filename = None
                            self.reset_recording_progress()
                            self.log('Recording ended')
                            try:
                                self.cache_file_list()
                            except Exception as e:
                                self.logger.exception(e)
                            self._probe_finished_recording(finished_name)
                            record_finished_file(self, finished_name)
                except Exception as e:
                    self.logger.exception(e)
                    nsfw_analyzer.finish_monitor(self)
                    try:
                        self.cache_file_list()
                    except Exception as e:
                        self.logger.exception(e)
                    self.log(self.status())
                    self.recording = False
                    self.recording_filename = None
                    self.reset_recording_progress()
                    self._sleep(self.sleep_on_error)
                    continue

                if self.quitting:
                    break
                elif self.bulk_update:
                    self._sleep(1)
                elif self.ratelimit:
                    self._sleep(self.sleep_on_ratelimit)
                elif offline_time > self.long_offline_timeout:
                    self._sleep(self.sleep_on_long_offline)
                elif self.sc == Status.PRIVATE:
                    self._sleep(self.sleep_on_private)
                else:
                    self._sleep(self.sleep_on_offline)

            self.sc = Status.NOTRUNNING
            self.log("Stopped")

    def setStatus(self, sc):
        if self.sc == Status.LONG_OFFLINE and sc == Status.OFFLINE:
            return
        self.sc = sc

    def getPlaylistVariants(self, url=None, m3u_data=None):
        sources = []

        if isinstance(m3u_data, m3u8.M3U8):
            variant_m3u8 = m3u_data
        elif isinstance(m3u_data, str):
            variant_m3u8 = m3u8.loads(m3u_data)
        elif not m3u_data or url:
            result = self.session.get(url, headers=self.headers, cookies=self.cookies)
            m3u8_doc = result.content.decode("utf-8")
            variant_m3u8 = m3u8.loads(m3u8_doc)
        else:
            return sources

        for playlist in variant_m3u8.playlists:
            stream_info = playlist.stream_info
            resolution = stream_info.resolution if type(stream_info.resolution) is tuple else (0, 0)
            sources.append({
                'url': playlist.uri,
                'resolution': resolution,
                'frame_rate': stream_info.frame_rate,
                'bandwidth': stream_info.bandwidth
            })

        if not variant_m3u8.is_variant and len(sources) >= 1:
            self.logger.warn("Not variant playlist, can't select resolution")
            return None
        return sources  # [(url, (width, height)),...]

    def getWantedResolutionPlaylist(self, url):
        try:
            sources = self.getPlaylistVariants(url)
            if sources is None:
                return None

            if len(sources) == 0:
                self.logger.error("No available sources")
                return None

            # Read the resolution setting at call time so changes made via the
            # settings API apply to the next recording without a restart.
            wanted_resolution = parameters.get_recording_resolution()
            wanted_preference = parameters.get_recording_resolution_preference()
            if wanted_resolution == parameters.RECORDING_RESOLUTION_MAX:
                # "max" always selects the highest available source: a huge target
                # height with the closest policy; preference is ignored here.
                target_resolution = 999999
                wanted_preference = 'closest'
            else:
                target_resolution = wanted_resolution

            for source in sources:
                width, height = source['resolution']
                if width < height:
                    source['resolution_diff'] = width - target_resolution
                else:
                    source['resolution_diff'] = height - target_resolution

            sources.sort(key=lambda a: abs(a['resolution_diff']))
            selected_source = None

            if wanted_preference == 'exact':
                if sources[0]['resolution_diff'] == 0:
                    selected_source = sources[0]
            elif wanted_preference == 'closest' or len(sources) == 1:
                selected_source = sources[0]
            elif wanted_preference == 'exact_or_least_higher':
                for source in sources:
                    if source['resolution_diff'] >= 0:
                        selected_source = source
                        break
            elif wanted_preference == 'exact_or_highest_lower':
                for source in sources:
                    if source['resolution_diff'] <= 0:
                        selected_source = source
                        break
            else:
                self.logger.error('Invalid value for recording resolution preference')
                return None

            if selected_source is None:
                self.logger.error("Couldn't select a resolution")
                return None

            if selected_source['resolution'][1] != 0:
                frame_rate = ''
                if selected_source['frame_rate'] is not None and selected_source['frame_rate'] != 0:
                    frame_rate = f" {selected_source['frame_rate']}fps"
                self.logger.info(f"Selected {selected_source['resolution'][0]}x{selected_source['resolution'][1]}{frame_rate} resolution")
                width, height = selected_source['resolution']
                self.recording_resolution = min(width, height) if width and height else (height or width or None)
            selected_source_url = selected_source['url']
            return urljoin(url, selected_source_url)
        except BaseException as e:
            self.logger.error("Can't get playlist, got some error: " + str(e))
            traceback.print_tb(e.__traceback__)
            return None

    def getVideoUrl(self):
        pass

    def progressInfo(self, p):
        if p['status'] == 'downloading':
            self.log("Downloading " + str(round(float(p['downloaded_bytes']) / float(p['total_bytes']) * 100, 1)) + "%")
        if p['status'] == 'finished':
            self.log("Show ended. File:" + p['filename'])

    @property
    def outputFolder(self):
        return str(os.path.join(parameters.get_download_dir(), self.username + ' [' + self.siteslug + ']'))

    @property
    def nonAdultFolder(self):
        # Where media_index.relocate_non_adult moves this streamer's videos
        # classified 'non-adult'; same folder name under the non-adult dir.
        return str(os.path.join(parameters.get_nonadult_dir(), self.username + ' [' + self.siteslug + ']'))

    @property
    def recycleFolder(self):
        # Where media_index.apply_nsfw_action ('recycle') moves this streamer's
        # videos; same folder name under the NSFW recycle-bin dir.
        return str(os.path.join(parameters.get_nsfw_recycle_dir(), self.username + ' [' + self.siteslug + ']'))

    def genOutFilename(self, create_dir=True):
        folder = self.outputFolder
        if create_dir:
            os.makedirs(folder, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = os.path.join(folder, f'{self.username}-{timestamp}.{CONTAINER}')
        return filename

    @classmethod
    def fromConfig(cls, data):
        instance = cls(username=data['username'])
        instance.running = data.get('running', True)
        instance.country = data.get('country')
        instance.gender = data.get('gender')
        instance.monitorState = data.get('monitorState', 'active')
        return instance

    def export(self):
        return {
            "site": self.site,
            "username": self.username,
            "running": self.running,
            "monitorState": self.monitorState,
            "country": self.country,
            "gender": self.gender.value if isinstance(self.gender, Enum) else self.gender,
        }

    @staticmethod
    def str2site(site: str):
        site = site.lower()
        for sitecls in LOADED_SITES:
            if site == sitecls.site.lower() or \
                    site == sitecls.siteslug.lower() or \
                    site in sitecls.aliases:
                return sitecls
        return None

    @staticmethod
    def createInstance(username: str, site: str = None):
        if site:
            site_cls = Bot.str2site(site)
            if site_cls:
                return site_cls(username)
            else:
                raise Exception('No such site')
        return None


class RoomIdBot(Bot):
    def __init__(self, username, room_id=None):
        self.room_id = None
        super().__init__(username)

        if room_id and username:
            self.room_id = room_id

        if self.room_id is None and username.isnumeric():  # Username might be the room ID
            username_real = self.getUsernameFromRoomId(username)
            if username_real is not None:  # Username might not be the room ID even though it is numeric
                self.logger.debug(f'Found username: {username_real}')
                self.room_id = username
                self.setUsername(username_real)

        if self.room_id is None:  # We need to get the room ID
            self.room_id = self.getRoomIdFromUsername(username)
            if self.room_id:
                self.logger.debug(f'Found room ID: {self.room_id}')

        if self.room_id is None:  # Still no room ID, streamer probably does not exist
            self.logger.warning(f'Room ID not found')
            self.sc = Status.NOTEXIST

        self.logger = self.getLogger()
        self.url = self.getWebsiteURL()

    @classmethod
    def fromConfig(cls, data):
        instance = cls(username=data['username'], room_id=data.get('room_id'))
        instance.running = data.get('running', True)
        instance.monitorState = data.get('monitorState', 'active')
        return instance

    def export(self):
        data = super().export()
        data['room_id'] = self.room_id
        return data

    def getRoomIdFromUsername(self, username):
        return None

    def getUsernameFromRoomId(self, room_id):
        return None
