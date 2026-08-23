import sys
from streamonitor.manager import Manager
from streamonitor.clean_exit import CleanExit
import streamonitor.log as log

if sys.platform != "win32":
    import readline


class CLIManager(Manager):
    def __init__(self, streamers):
        super().__init__(streamers)
        self.logger = log.Logger("manager_cli")

    def run(self):
        while True:
            try:
                line = input("> ")
            except EOFError:
                self.do_quit()
                return
            # One bad command must not kill the console for the whole session.
            try:
                reply = self.execCmd(line)
            except Exception as e:
                self.logger.error(f'Command failed: {e}')
                reply = f'Error: {e}'
            if line == "quit":
                return
            self.logger.info(reply)

    def do_quit(self, _=None, __=None, ___=None):
        CleanExit(self.streamers)()
