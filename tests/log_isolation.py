"""Helpers that keep the process-global logging state isolated between tests.

``logging`` is process-global state: the root logger's level, its handlers and
the log record factory are all shared by every test in a session. The settings
API and :func:`chime_deck.config.configure_logging` install a scrubbing
factory and may register a secret, two file sinks, and change the level, so a
test that does any of that must put the previous state back.

Owning ticket: HSD-91z1.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from chime_deck.config import SECRET_SCRUBBER

__all__ = ["preserve_log_state"]


@contextmanager
def preserve_log_state() -> Iterator[None]:
    """Restore the root logger, its handlers and the log record factory.

    On entry the root level, a copy of ``root.handlers`` and the current record
    factory are snapshotted and every registered secret is forgotten. On exit
    the handlers added since are removed and **closed** (a leaked
    ``RotatingFileHandler`` raises ``ResourceWarning``, which is an error here),
    the original handlers, level and factory are restored, and the registered
    secrets are cleared again.

    Handlers that were already attached are never closed: closing pytest's
    capture handler would break every later test.

    :yields: control to the test body.
    """
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    factory = logging.getLogRecordFactory()
    SECRET_SCRUBBER.clear()
    try:
        yield
    finally:
        SECRET_SCRUBBER.clear()
        for handler in root.handlers:
            if any(handler is known for known in handlers):
                continue
            root.removeHandler(handler)
            handler.close()
        root.handlers[:] = handlers
        root.setLevel(level)
        logging.setLogRecordFactory(factory)
