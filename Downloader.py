import os
import sys
import streamonitor.config as config
from parameters import ZMQ_ENABLED
from streamonitor.managers.bulk_status_manager import BulkStatusManager
from streamonitor.managers.httpmanager import HTTPManager
from streamonitor.managers.climanager import CLIManager
from streamonitor.managers.zmqmanager import ZMQManager
from streamonitor.managers.outofspace_detector import OOSDetector
from streamonitor.clean_exit import CleanExit
from streamonitor.temp_repair import repair_leftover_temp_files
import streamonitor.sites  # must have

        
def is_docker():
    path = '/proc/self/cgroup'
    if os.path.exists('/.dockerenv'):
        return True
    if os.path.isfile(path):
        with open(path) as f:
            return any('docker' in line for line in f)
    return False


def main():
    # Disk space check disabled to allow running on low disk space
    # if not OOSDetector.disk_space_good():
    #     print(OOSDetector.under_threshold_message)
    #     sys.exit(1)

    # Repair temp files left behind by an interrupted recording before loading
    # streamers so a new recording can't collide with an old leftover.
    repair_leftover_temp_files()

    streamers = config.loadStreamers()

    clean_exit = CleanExit(streamers)

    oos_detector = OOSDetector(streamers)
    oos_detector.start()

    bulk_status_manager = BulkStatusManager(streamers)
    bulk_status_manager.start()

    if not is_docker():
        console_manager = CLIManager(streamers)
        console_manager.start()

    if ZMQ_ENABLED:
        zmq_manager = ZMQManager(streamers)
        zmq_manager.start()

    http_manager = HTTPManager(streamers)
    http_manager.start()


main()
