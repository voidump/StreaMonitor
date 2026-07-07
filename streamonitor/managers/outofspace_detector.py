import os.path
import shutil
from time import sleep
import streamonitor.log as log
from threading import Thread
import parameters
from parameters import MIN_FREE_DISK_PERCENT
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
        download_dir = parameters.get_download_dir()
        if os.path.exists(download_dir):
            usage = shutil.disk_usage(download_dir)
        else:
            usage = shutil.disk_usage('.')
        return usage

    @staticmethod
    def disk_space_good():
        return OOSDetector.free_space() > MIN_FREE_DISK_PERCENT

    def run(self):
        while True:
            if not self.disk_space_good():
                self.logger.warning(self.under_threshold_message)
                CleanExit(self.streamers)()
                return
            sleep(5)
