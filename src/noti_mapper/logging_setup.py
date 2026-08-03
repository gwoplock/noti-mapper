"""Structured logging to stderr, for journald.

No metrics endpoint. The journal is the observability surface, and the target
is that a user can answer "why did this fire three weeks ago" from
``journalctl`` alone. Every state transition logs the new value, the cause, the
originating instance name, the rule name, event metadata, and the outcome of
each output push.

Two things make that work in practice:

* Structured extras are appended as ``key=value`` pairs, so a log line carries
  the rule and instance names rather than burying them in prose.
* When running under systemd, lines are prefixed with a ``<N>`` syslog priority
  so journald files them at the right level instead of calling everything on
  stderr an error.
"""

import logging

# Attributes every LogRecord has. Anything else on a record came from an
# `extra=` argument and is worth printing.
_STANDARD_RECORD_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class StructuredFormatter(logging.Formatter):
    """``message key=value ...``, optionally with a journald priority prefix."""

    def __init__(self, *, journal: bool) -> None:
        super().__init__()
        self._journal = journal

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        extras = _extras_of(record)
        if extras:
            message = f"{message} {extras}"

        if record.exc_info is not None:
            message = f"{message}\n{self.formatException(record.exc_info)}"

        if self._journal:
            return f"<{priority_for(record.levelno).value}>{message}"
        return f"{record.levelname:<8} {record.name}: {message}"


def _extras_of(record: logging.LogRecord) -> str:
    parts: list[str] = []
    for key in sorted(vars(record)):
        if key in _STANDARD_RECORD_ATTRIBUTES or key.startswith("_"):
            continue
        parts.append(f"{key}={vars(record)[key]!r}")
    return " ".join(parts)
