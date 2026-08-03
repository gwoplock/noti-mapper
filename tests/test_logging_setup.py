import io
import logging

from noti_mapper.logging_setup import (
    StructuredFormatter,
)


def _record(
    *, level: int = logging.INFO, message: str = "hello", **extras: object
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="noti_mapper.engine",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extras.items():
        setattr(record, key, value)
    return record


def test_extras_are_appended_as_key_value_pairs() -> None:
    formatter = StructuredFormatter(journal=False)
    line = formatter.format(_record(rule="Package On Porch", instance="Porch Mail", state=True))

    assert "hello" in line
    assert "rule='Package On Porch'" in line
    assert "instance='Porch Mail'" in line
    assert "state=True" in line


def test_extras_are_sorted_so_lines_are_diffable() -> None:
    formatter = StructuredFormatter(journal=False)
    line = formatter.format(_record(zulu=1, alpha=2))
    assert line.index("alpha=") < line.index("zulu=")


def test_a_record_with_no_extras_is_just_the_message() -> None:
    formatter = StructuredFormatter(journal=False)
    line = formatter.format(_record())
    assert line.endswith("noti_mapper.engine: hello")


def test_exceptions_are_included() -> None:
    formatter = StructuredFormatter(journal=False)
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        import sys

        record = _record(level=logging.ERROR)
        record.exc_info = sys.exc_info()

    line = formatter.format(record)
    assert "RuntimeError: kaboom" in line
    assert "Traceback" in line


def test_configured_logging_writes_extras_to_the_stream() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream=stream)
    handler.setFormatter(StructuredFormatter(journal=False))
    logger = logging.getLogger("test.structured")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("rule latched", extra={"rule": "R", "cause": "Mail"})
    finally:
        logger.removeHandler(handler)

    written = stream.getvalue()
    assert "rule latched" in written
    assert "cause='Mail'" in written
