import math
import shutil
import time
from functools import wraps
from logging import INFO, WARN, ERROR
from threading import RLock, Thread
from termcolor import colored
import terminaltables.terminal_io
from terminaltables import AsciiTable
import streamonitor.config as config
import streamonitor.log as log
from streamonitor.bot import Bot, LOADED_SITES
from streamonitor.managers.outofspace_detector import OOSDetector

from streamonitor.enums import Status

# One process-wide lock: every Manager flavor (CLI/ZMQ/HTTP/bulk) shares the
# same streamers list, and Flask serves requests from multiple threads. All
# command handlers and saveConfig hold it, so a save can never iterate the
# list mid-mutation (which used to drop an unrelated streamer from
# config.json) and two commands can never interleave their mutations.
_commands_lock = RLock()


def _locked(fn):
    # RLock so nested commands (do_restart -> do_stop/do_start) just re-enter.
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _commands_lock:
            return fn(*args, **kwargs)
    return wrapper


class Manager(Thread):
    def __init__(self, streamers):
        super().__init__()
        self.daemon = True
        self.streamers = streamers
        self.logger = log.Logger("manager")

    def execCmd(self, line):
        parts = str(line).split(' ')
        if 'do_' + parts[0] not in dir(self):
            return 'Unknown command'

        command = getattr(self, 'do_' + parts[0])
        if command:
            username = parts[1] if len(parts) > 1 else ""
            site = parts[2] if len(parts) > 2 else ""
            streamer = self.getStreamer(username, site)
            return command(streamer, username, site)

    @_locked
    def getStreamer(self, username, site):
        found = None
        site = Bot.str2site(site)
        if site:
            site = site.site
        for streamer in self.streamers:
            if streamer.username == username:
                if site and site != "":
                    if streamer.site == site:
                        return streamer
                else:
                    if not found:
                        found = streamer
                    else:
                        self.logger.error('Multiple users exist with this username, specify site too')
                        return None
        return found

    @_locked
    def saveConfig(self):
        config.save_config([s.export() for s in self.streamers])

    @_locked
    def do_add(self, streamer, username, site):
        if streamer:
            return 'Streamer already exists'
        elif username and site:
            try:
                streamer = Bot.createInstance(username, site)
                self.streamers.append(streamer)
                streamer.start()
                streamer.restart()
                self.saveConfig()
                return "Added [" + streamer.siteslug + "] " + streamer.username
            except Exception as e:
                return f"Failed to add: {e}"
        else:
            return "Missing value(s)"

    @_locked
    def do_remove(self, streamer, username, site):
        if not streamer:
            return "Streamer not found"
        try:
            # thread_too: the bot leaves the streamers list, so CleanExit can
            # never reach it again — without ending the thread here it would
            # spin in run() forever.
            streamer.stop(None, None, thread_too=True)
            streamer.logger.handlers = []
            self.streamers.remove(streamer)
            self.saveConfig()
            return "OK"
        except Exception as e:
            self.logger.error(e)
            return "Failed to remove streamer"

    @_locked
    def do_pause(self, streamer, username, site):
        # Layer 1: temporarily stop recording, keep the entry as-is
        # (monitorState stays 'active'; the paused state is derived from running).
        if not streamer:
            return "Streamer not found"
        return self.do_stop(streamer, username, site)

    @_locked
    def do_resume(self, streamer, username, site):
        # Layer 1: resume a paused streamer.
        if not streamer:
            return "Streamer not found"
        return self.do_start(streamer, username, site)

    @_locked
    def do_stop_recording(self, streamer, username, site):
        # Layer 2: archive the streamer. Stop the bot and mark it stopped, but
        # keep the config entry (name/link/local files) for a one-click restart.
        if not streamer:
            return "Streamer not found"
        streamer.monitorState = 'stopped'
        return self.do_stop(streamer, username, site)

    @_locked
    def do_reactivate(self, streamer, username, site):
        # Layer 2 inverse: restore an archived streamer and start it again.
        if not streamer:
            return "Streamer not found"
        streamer.monitorState = 'active'
        return self.do_start(streamer, username, site)

    @_locked
    def do_purge(self, streamer, username, site, delete_files=False):
        # Layer 3: permanent delete. Stop the bot, record it in history, drop it
        # from config and optionally remove its recordings on disk.
        if not streamer:
            return "Streamer not found"
        try:
            # thread_too for the same reason as do_remove: the bot is dropped
            # from the list, nothing else will ever end its thread.
            streamer.stop(None, None, thread_too=True)
            streamer.logger.handlers = []
            config.add_history_entry({
                'site': streamer.site,
                'siteslug': streamer.siteslug,
                'username': streamer.username,
                'url': streamer.url,
                'deletedAt': int(time.time()),
            })
            if delete_files:
                shutil.rmtree(streamer.outputFolder, ignore_errors=True)
            self.streamers.remove(streamer)
            self.saveConfig()
            return "OK"
        except Exception as e:
            self.logger.error(e)
            return "Failed to delete streamer"

    @_locked
    def do_start(self, streamer, username, site):
        if not streamer:
            if username == '*':
                for streamer in self.streamers:
                    if not streamer.is_alive():
                        streamer.start()
                    streamer.restart()
                self.saveConfig()
                return "Started all"
            else:
                return "Streamer not found"
        else:
            try:
                if not streamer.is_alive():
                    streamer.start()
                streamer.restart()
                self.saveConfig()
                return "OK"
            except Exception as e:
                self.logger.error(e)
                return "Failed to start"

    @_locked
    def do_stop(self, streamer, username, site):
        if not streamer:
            if username == '*':
                for streamer in self.streamers:
                    streamer.stop(None, None)
                self.saveConfig()
                return "Stopped all"
            else:
                return "Streamer not found"
        else:
            try:
                streamer.stop(None, None)
                self.saveConfig()
                return "OK"
            except Exception as e:
                self.logger.error(e)
                return "Failed to stop"

    @_locked
    def do_restart(self, streamer, username, site):
        if not streamer:
            return "Streamer not found"
        self.do_stop(streamer, username, site)
        return self.do_start(streamer, username, site)
        
    @_locked
    def do_status(self, streamer, username, site):
        output = [["Username", "Site", "Started", "Status"]]

        def line():
            output.append([streamer.username,
                           streamer.site,
                           streamer.running,
                           streamer.status()])

        if streamer:
            line()
        else:
            for streamer in self.streamers:
                line()
        return "Status:\n" + f'Free space: {str(round(OOSDetector.free_space(), 3))}%\n\n' + AsciiTable(output).table

    @_locked
    def do_status2(self, streamer, username, site):
        maxlen = max([len(s.username) for s in self.streamers] or [0])
        termwidth = terminaltables.terminal_io.terminal_size()[0]
        table_nx = math.floor(termwidth/(maxlen+3))
        output = ''
        output += 'Status:\n'

        for site in LOADED_SITES:
            output += site.site + '\n'
            output += ('+' + '-'*(maxlen+2))*table_nx + '+\n'
            site_name = site.site
            i = 0
            for streamer in self.streamers:
                if streamer.site == site_name:
                    output += '!'
                    status_color = None
                    status = streamer.sc
                    if status == Status.PUBLIC: status_color = 'green'
                    if status == Status.PRIVATE: status_color = 'magenta'
                    if status == Status.ERROR: status_color = 'red'
                    if not streamer.running: status_color = 'grey'
                    output += colored(' ' + streamer.username + ' '*(maxlen-len(streamer.username)) + ' ', status_color)
                    i += 1
                    if i == table_nx:
                        output += '!\n'
                        i = 0
            for r in range(i, table_nx):
                output += '! ' + ' ' * maxlen + ' '
            output += '!\n'
            output += ('+' + '-'*(maxlen+2))*table_nx + '+\n'
            output += '\n'
        return output
