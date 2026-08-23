import shutil
from unittest import mock

import pytest

import streamonitor.managers.outofspace_detector as oos_mod
import streamonitor.managers.zmqmanager as zmq_mod
from streamonitor.managers.climanager import CLIManager


class _Done(Exception):
    pass


class FakeSocket:
    def __init__(self, lines):
        self.lines = list(lines)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def bind(self, addr):
        pass

    def recv_string(self):
        if not self.lines:
            raise _Done()
        return self.lines.pop(0)

    def send_string(self, s):
        self.sent.append(s)


def test_zmq_survives_command_exception_and_always_replies(monkeypatch):
    # A do_* handler raising used to kill the ZMQ thread; worse, dying between
    # recv and send leaves the REP socket in EFSM. Every recv must get a send.
    sock = FakeSocket(['explode', 'explode'])
    fake_zmq = mock.Mock()
    fake_zmq.Context.instance.return_value.socket.return_value = sock
    monkeypatch.setattr(zmq_mod, 'zmq', fake_zmq)
    manager = zmq_mod.ZMQManager([])
    monkeypatch.setattr(manager, 'execCmd',
                        mock.Mock(side_effect=RuntimeError('boom')))
    with pytest.raises(_Done):
        manager.run()
    assert len(sock.sent) == 2
    assert all('boom' in reply for reply in sock.sent)


def test_cli_survives_command_exception(monkeypatch):
    lines = iter(['explode', 'quit'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(lines))
    manager = CLIManager([])
    monkeypatch.setattr(manager, 'execCmd', mock.Mock(
        side_effect=[RuntimeError('boom'), 'Bye']))
    manager.run()  # must return via 'quit', not die on the exception
    assert manager.execCmd.call_count == 2


def test_oos_watchdog_survives_oserror(monkeypatch):
    detector = oos_mod.OOSDetector([])
    checks = []

    def bad_check():
        checks.append(1)
        raise OSError('external disk vanished')

    monkeypatch.setattr(oos_mod.OOSDetector, 'disk_space_good',
                        staticmethod(bad_check))

    def fake_sleep(_):
        if len(checks) >= 2:
            raise _Done()

    monkeypatch.setattr(oos_mod, 'sleep', fake_sleep)
    with pytest.raises(_Done):
        detector.run()
    # The loop kept checking after the first OSError instead of dying silently.
    assert len(checks) >= 2


def test_space_usage_falls_back_to_cwd(monkeypatch):
    sentinel = shutil.disk_usage('.')

    def picky_disk_usage(path):
        if path == '.':
            return sentinel
        raise OSError('transient mount failure')

    monkeypatch.setattr(oos_mod.shutil, 'disk_usage', picky_disk_usage)
    assert oos_mod.OOSDetector.space_usage() == sentinel
