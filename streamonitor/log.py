import logging
import parameters


class Logger(object):
    def __init__(self, name="__name__"):
        self.name = name
        self.loglevel = logging.DEBUG if parameters.DEBUG else logging.INFO
        self.logger = logging.getLogger(self.name)
        self.logger.setLevel(self.loglevel)
        # Register the handler once per logger name: logging.getLogger returns
        # a process-wide singleton, and stacking a new StreamHandler on every
        # Logger construction made shared names (e.g. "manager") print each
        # record as many times as they were constructed.
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(levelname)s - {}: %(message)s'.format(name)))
            self.logger.addHandler(handler)
        self.handler = self.logger.handlers[0]

    def get_logger(self):
        return self.logger

    def debug(self, msg):
        self.logger.debug(msg)

    def warning(self, msg):
        self.logger.warning(msg)

    def error(self, msg):
        self.logger.error(msg)

    def info(self, msg):
        self.logger.info(msg)
