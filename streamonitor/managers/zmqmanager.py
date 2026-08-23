import zmq
from parameters import ZMQ_HOST, ZMQ_PORT
from streamonitor.manager import Manager
import streamonitor.log as log


class ZMQManager(Manager):
    def __init__(self, streamers):
        super().__init__(streamers)
        self.logger = log.Logger("manager_zmq")

    def run(self):
        with zmq.Context.instance().socket(zmq.REP) as socket:
            socket.bind("tcp://%s:%d" % (ZMQ_HOST, ZMQ_PORT))
            while True:
                line = socket.recv_string()
                self.logger.info("[ZMQ] " + line)
                # REP sockets require strict recv/send alternation: dying (or
                # skipping the send) after a recv leaves the socket in EFSM,
                # so every command must produce exactly one reply.
                try:
                    reply = self.execCmd(line)
                except Exception as e:
                    self.logger.error(f'Command failed: {e}')
                    reply = f'Error: {e}'
                socket.send_string(reply if type(reply) is str else '')
