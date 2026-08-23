import logging

import streamonitor.log as log


def test_same_name_logger_registers_single_handler():
    # Constructing Logger repeatedly (and calling get_logger) for the same name
    # must not stack StreamHandlers: previously every construction added one,
    # so the shared "manager" logger printed each record 5 times.
    name = 'test-idempotent-handler'
    log.Logger(name)
    log.Logger(name).get_logger()
    log.Logger(name).get_logger()
    assert len(logging.getLogger(name).handlers) == 1


def test_different_names_get_their_own_handler():
    a = log.Logger('test-handler-a').get_logger()
    b = log.Logger('test-handler-b').get_logger()
    assert len(a.handlers) == 1
    assert len(b.handlers) == 1
    assert a.handlers[0] is not b.handlers[0]
