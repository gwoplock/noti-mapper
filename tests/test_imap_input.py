"""The IMAP reader itself, without a mail server.

Only the parts that run before anything is connected. The matching rules live
in ``test_imap_matching``, and the network side is covered end to end by the
runtime tests with the probe plugins.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from imap_input import ImapInput
from noti_mapper.plugin import ObservedEvent
from noti_mapper.storage import Database, database_path, initialize
from tests.support import make_context

BASE: dict[str, object] = {
    "host": "mail.example.net",
    "username": "user@example.net",
    "password": "hunter2",
}


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    opened = Database(path=database_path(tmp_path / "state"))
    initialize(opened)
    try:
        yield opened
    finally:
        opened.close()


def _build(database: Database, **overrides: object) -> ImapInput:
    settings = dict(BASE)
    settings.update(overrides)

    def emit(event: ObservedEvent) -> None:
        del event
        raise AssertionError("nothing should be emitted before the reader is started")

    return ImapInput(
        context=make_context(instance_name="Porch Mail", database=database, settings=settings),
        emit=emit,
    )


def test_matching_everything_is_a_warning(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        _build(database)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "every message" in message
    # The message has to name the keys, or the reader is left to guess.
    assert '"senders"' in message
    assert '"subject_patterns"' in message


def test_leaving_out_only_the_allowlist_is_an_info_line(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        _build(database, subject_patterns=["^Delivered:"])

    assert [record.levelname for record in caplog.records] == ["INFO"]
    assert "no sender allowlist" in caplog.records[0].getMessage()


def test_leaving_out_only_the_patterns_is_an_info_line(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        _build(database, senders=["ups.com"])

    assert [record.levelname for record in caplog.records] == ["INFO"]
    assert "no subject patterns" in caplog.records[0].getMessage()


def test_a_fully_constrained_reader_says_nothing(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        _build(database, senders=["ups.com"], subject_patterns=["^Delivered:"])

    assert caplog.records == []
