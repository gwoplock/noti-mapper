"""The setup code encoding and the file it is kept in.

No accessory and no driver here, which is the reason ``pairing`` is a separate
module: the encoding is arithmetic and the file is a file, and neither needs
mDNS brought up to be checked.

The expected URIs below were produced by HAP-python's own payload construction
run against the ``base36`` package, and agreed with this implementation across
20,008 integers and 2,004 setup codes. They are written out here so that
agreement is not something the suite has to take on trust.
"""

import datetime
import stat
from pathlib import Path

import pytest

from homekit_output.pairing import (
    base36_encode,
    setup_code_text,
    setup_uri,
    write_setup_code_file,
)

WRITTEN_AT = datetime.datetime(2026, 8, 9, 20, 15, tzinfo=datetime.UTC)


# -- base 36 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (1, "1"),
        (9, "9"),
        (10, "A"),
        (35, "Z"),
        (36, "10"),
        (1295, "ZZ"),
        (1296, "100"),
        (2**27, "27WR28"),
        (2**31 - 1, "ZIK0ZJ"),
    ],
)
def test_base36_encodes_known_values(value: int, expected: str) -> None:
    assert base36_encode(value) == expected


# -- the setup URI ------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("518-08-582", "X-HM://0081F45S67OSX"),
        ("111-11-111", "X-HM://0080QVVEV7OSX"),
        ("000-00-000", "X-HM://0080K9Q0W7OSX"),
        ("999-99-999", "X-HM://00827T2IN7OSX"),
    ],
)
def test_the_setup_uri_matches_the_reference_encoding(code: str, expected: str) -> None:
    assert setup_uri(setup_code=code, setup_id="7OSX") == expected


def test_the_payload_is_always_nine_digits_and_the_setup_id_four() -> None:
    uri = setup_uri(setup_code="000-00-000", setup_id="7OSX")
    payload = uri.removeprefix("X-HM://")
    assert len(payload) == 9 + 4
    # A small payload is padded rather than shortened, or the reader misreads
    # where the setup id starts.
    assert payload.startswith("008")


def test_the_dashes_in_a_setup_code_are_not_part_of_the_number() -> None:
    assert setup_uri(setup_code="518-08-582", setup_id="7OSX") == setup_uri(
        setup_code="51808582", setup_id="7OSX"
    )


# -- the file's contents ------------------------------------------------------


def _text(paired: bool) -> str:
    return setup_code_text(
        instance_name="Porch Lamp",
        display_name="Package Waiting",
        setup_code="518-08-582",
        uri="X-HM://0081F45S67OSX",
        paired=paired,
        written_at=WRITTEN_AT,
    )


def test_an_unpaired_accessory_gets_instructions() -> None:
    text = _text(paired=False)
    assert "518-08-582" in text
    assert "X-HM://0081F45S67OSX" in text
    assert "Not paired as of 2026-08-09T20:15:00+00:00" in text
    assert "Add Accessory" in text
    # The reassurance that makes people willing to try this at all.
    assert "nothing you already own is affected" in text


def test_a_paired_accessory_says_when_the_code_is_still_needed() -> None:
    text = _text(paired=True)
    assert "Paired as of 2026-08-09T20:15:00+00:00" in text
    assert "remove the accessory from the Home app" in text
    # Kept, because pairing again needs it.
    assert "518-08-582" in text


def test_the_file_names_the_instance_and_the_accessory() -> None:
    text = _text(paired=False)
    assert "'Porch Lamp'" in text
    assert "Package Waiting" in text


def test_the_file_warns_that_the_code_is_a_credential() -> None:
    assert "can pair with it" in _text(paired=False)


# -- writing it ---------------------------------------------------------------


def test_a_new_file_is_created_readable_only_by_its_owner(tmp_path: Path) -> None:
    path = tmp_path / "setup-code.txt"

    write_setup_code_file(path, "hello\n")

    assert path.read_text(encoding="utf-8") == "hello\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_existing_file_is_replaced_and_its_mode_reasserted(tmp_path: Path) -> None:
    path = tmp_path / "setup-code.txt"
    path.write_text("an older, longer code file\n", encoding="utf-8")
    path.chmod(0o644)

    write_setup_code_file(path, "new\n")

    # O_CREAT's mode applies only when creating, so an existing wide-open file
    # would otherwise stay wide open.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Truncated, not overwritten in place leaving a tail of the old contents.
    assert path.read_text(encoding="utf-8") == "new\n"
