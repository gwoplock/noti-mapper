"""Shared fixtures."""

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Undo any logging configuration a test performed.

    ``noti-mapper``'s CLI installs a handler on the root logger pointed at
    stderr. Under pytest that stderr is a capture buffer belonging to the test
    that ran, so leaving the handler in place makes a later test's log call
    write to a closed stream.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
