from noti_mapper.names import (
    MAX_NAME_LENGTH,
    find_name_problems,
    normalize_name,
)


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


def test_interior_whitespace_is_preserved() -> None:
    assert normalize_name("Porch  Mail") == "Porch  Mail"


def test_length_is_measured_after_stripping() -> None:
    padded = "  " + "x" * MAX_NAME_LENGTH + "  "
    assert find_name_problems(padded) == []
