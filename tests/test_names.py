import pytest

from noti_mapper.names import (
    MAX_NAME_LENGTH,
    InvalidNameError,
    find_name_problems,
    normalize_name,
    uniqueness_key,
    validate_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "Porch Mail",
        "porch-mail",
        "porch_mail",
        "porch.mail.2",
        "A",
        "Package On Porch",
        "Café Mail",
        "x" * MAX_NAME_LENGTH,
    ],
)
def test_valid_names_are_accepted(name: str) -> None:
    assert find_name_problems(name) == []
    assert validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "porch/mail",
        "porch\\mail",
        'porch"mail"',
        "porch'mail'",
        "porch\x00mail",
        "porch\nmail",
        "porch\tmail",
        "porch:mail",
        "porch;mail",
        "porch*mail",
        "porch$mail",
        "${secret:x}",
        "porch mail",  # non-breaking space
    ],
)
def test_invalid_characters_are_rejected(name: str) -> None:
    problems = find_name_problems(name)
    assert problems, f"{name!r} should have been rejected"
    assert "disallowed characters" in problems[0]
    with pytest.raises(InvalidNameError):
        validate_name(name)


def test_empty_and_whitespace_only_names_are_rejected() -> None:
    assert find_name_problems("") == ["name is empty"]
    assert find_name_problems("   ") == ["name is empty"]


def test_too_long_names_are_rejected() -> None:
    problems = find_name_problems("x" * (MAX_NAME_LENGTH + 1))
    assert len(problems) == 1
    assert "129 characters" in problems[0]


def test_all_problems_are_reported_in_one_pass() -> None:
    problems = find_name_problems("/" * (MAX_NAME_LENGTH + 1))
    assert len(problems) == 2


def test_leading_and_trailing_whitespace_is_stripped() -> None:
    assert normalize_name("  Porch Mail  ") == "Porch Mail"
    assert normalize_name("\tPorch Mail\n") == "Porch Mail"
    assert validate_name("  Porch Mail  ") == "Porch Mail"


def test_interior_whitespace_is_preserved() -> None:
    assert normalize_name("Porch  Mail") == "Porch  Mail"


def test_length_is_measured_after_stripping() -> None:
    padded = "  " + "x" * MAX_NAME_LENGTH + "  "
    assert find_name_problems(padded) == []


def test_uniqueness_key_is_case_insensitive() -> None:
    assert uniqueness_key("Porch Mail") == uniqueness_key("porch mail")
    assert uniqueness_key("Porch Mail") != uniqueness_key("Porch Mai")


def test_uniqueness_key_normalizes_unicode_composition() -> None:
    precomposed = "Café"
    decomposed = "Café"
    assert precomposed != decomposed
    assert uniqueness_key(precomposed) == uniqueness_key(decomposed)
