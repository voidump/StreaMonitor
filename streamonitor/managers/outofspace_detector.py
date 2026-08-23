import os
import os.path
import shutil
import time
from contextlib import closing
from time import sleep
import streamonitor.log as log
from threading import Thread
import parameters
from parameters import MIN_FREE_DISK_PERCENT
from streamonitor import media_index
from streamonitor.clean_exit import CleanExit


class OOSDetector(Thread):
    under_threshold_message = 'Free space is under threshold. Exiting.'

    def __init__(self, streamers):
        super().__init__()
        self.streamers = streamers
        self.daemon = True
        self.logger = log.Logger("out_of_space_detector")

    @staticmethod
    def free_space():
        usage = OOSDetector.space_usage()
        free_percent = usage.free / usage.total * 100
        return free_percent

    @staticmethod
    def space_usage():
        # No exists() pre-check: it is a TOCTOU against disk_usage, and a
        # transiently unavailable mount (external/network disk) must fall back
        # instead of raising into the watchdog.
        download_dir = parameters.get_download_dir()
        try:
            return shutil.disk_usage(download_dir)
        except OSError:
            return shutil.disk_usage('.')

    @staticmethod
    def disk_space_good():
        return OOSDetector.free_space() > MIN_FREE_DISK_PERCENT

    @staticmethod
    def _non_adult_videos():
        # Read-only index query, oldest first. Only rows explicitly classified
        # 'non-adult' by the NSFW pipeline qualify: 'adult' and 'unknown' are
        # never deletion candidates. Returns [] when the index is disabled or
        # missing (no classifications exist, so there is nothing to clean).
        if not parameters.MEDIA_INDEX_ENABLED or not os.path.isfile(media_index.index_db_path()):
            return []
        with closing(media_index.connect()) as conn:
            rows = conn.execute(
                "SELECT abs_path FROM media_files "
                "WHERE deleted=0 AND classification='non-adult' "
                "ORDER BY mtime ASC").fetchall()
        return [row[0] for row in rows]

    def _active_recording_dirs(self):
        # (abs outputFolder, username) of streamers recording right now; same
        # username-prefix protection as media_index._cleanup_undersized, since
        # actual on-disk names can differ from recording_filename (ffmpeg
        # strftime segments, VR suffixes, hls _%03d parts).
        active = []
        for streamer in self.streamers or []:
            try:
                if getattr(streamer, 'recording', False):
                    active.append((os.path.abspath(streamer.outputFolder), streamer.username))
            except Exception:
                continue
        return active

    def _deletable(self, abs_path, active):
        name = os.path.basename(abs_path)
        if '.tmp.' in name:
            return False
        try:
            stat = os.stat(abs_path)
        except OSError:
            media_index.mark_deleted(abs_path)  # already gone from disk
            return False
        # Actively written files keep a fresh mtime; never touch them.
        if time.time() - stat.st_mtime < media_index.CLEANUP_MTIME_GRACE_SECONDS:
            return False
        folder = os.path.dirname(os.path.abspath(abs_path))
        return not any(folder == rec_folder and name.startswith(rec_user)
                       for rec_folder, rec_user in active)

    def cleanup_non_adult(self):
        # Delete non-adult videos (oldest first) until free space is back
        # above the cleanup threshold. Runs before the exit check, so the
        # original exit behavior still applies if this cannot free enough.
        threshold = parameters.OOS_CLEANUP_THRESHOLD_PERCENT
        if self.free_space() > threshold:
            return
        try:
            candidates = self._non_adult_videos()
        except Exception as e:
            self.logger.warning(f'Non-adult cleanup: index query failed: {e}')
            return
        if not candidates:
            return
        active = self._active_recording_dirs()
        deleted = 0
        freed = 0
        for abs_path in candidates:
            if self.free_space() > threshold:
                break
            if not self._deletable(abs_path, active):
                continue
            try:
                size = os.path.getsize(abs_path)
                os.remove(abs_path)
            except OSError as e:
                self.logger.warning(f'Non-adult cleanup: cannot delete {abs_path}: {e}')
                continue
            media_index.mark_deleted(abs_path)
            deleted += 1
            freed += size
            self.logger.info(f'Low disk space: deleted non-adult video {abs_path} ({size} bytes)')
        if deleted:
            self.logger.info(
                f'Non-adult cleanup: deleted {deleted} video(s), freed {freed} bytes, '
                f'free space now {round(self.free_space(), 3)}%')

    def run(self):
        # The whole iteration is guarded: this thread is the low-disk
        # protection, and a single transient OSError silently killing it would
        # disable exactly the safeguard it exists to provide.
        while True:
            try:
                if parameters.OOS_CLEANUP_NON_ADULT:
                    try:
                        self.cleanup_non_adult()
                    except Exception as e:
                        self.logger.warning(f'Non-adult cleanup failed: {e}')
                if not self.disk_space_good():
                    self.logger.warning(self.under_threshold_message)
                    CleanExit(self.streamers)()
                    return
            except Exception as e:
                self.logger.warning(f'Disk watchdog check failed: {e}')
            sleep(5)
