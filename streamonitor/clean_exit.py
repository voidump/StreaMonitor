import os
import time
import signal
from threading import Thread

import streamonitor.log as log


class CleanExit:
    class DummyThread(Thread):
        def __init__(self):
            super().__init__()
            self._stop = False

        def run(self):
            while True:
                if self._stop:
                    return
                time.sleep(1)

        def stop(self):
            self._stop = True

    dummy_thread = DummyThread()

    # Total seconds to wait for bot threads before forcing the process down.
    # The process must always be able to exit: past this deadline a stuck
    # downloader would otherwise hang shutdown until an external SIGKILL, which
    # corrupts any mp4 being written.
    shutdown_timeout = 30

    def __init__(self, streamers):
        self.streamers = streamers
        if not self.dummy_thread.is_alive():
            self.dummy_thread.start()
            signal.signal(signal.SIGINT, self.clean_exit)
            signal.signal(signal.SIGTERM, self.clean_exit)
            signal.signal(signal.SIGABRT, self.clean_exit)

    def __call__(self, *args, **kwargs):
        self.clean_exit()

    def clean_exit(self, _=None, __=None):
        logger = log.Logger('clean_exit')
        for streamer in self.streamers:
            # One failing stop() must not prevent the remaining streamers from
            # being told to shut down.
            try:
                streamer.stop(None, None, True)
            except Exception as e:
                logger.error(f'Failed to stop {getattr(streamer, "username", streamer)}: {e}')
        deadline = time.time() + self.shutdown_timeout
        for streamer in self.streamers:
            while streamer.is_alive() and time.time() < deadline:
                time.sleep(0.2)
        stuck = [getattr(s, 'username', repr(s)) for s in self.streamers if s.is_alive()]
        self.dummy_thread.stop()
        if stuck:
            logger.error(
                f'Threads still alive after {self.shutdown_timeout}s, forcing exit: {stuck}')
            os._exit(1)
