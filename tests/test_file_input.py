from file_input import FileInput

# -- settings -----------------------------------------------------------------


def _validate(**overrides: object) -> list[str]:
    settings: dict[str, object] = {"path": "/var/spool/notify"}
    settings.update(overrides)
    return FileInput.validate_settings(settings)


def test_a_minimal_configuration_validates() -> None:
    assert _validate() == []


def test_the_path_is_required_and_must_be_absolute() -> None:
    assert FileInput.validate_settings({}) == ['"path" is required']
    assert any("must be absolute" in problem for problem in _validate(path="relative/path"))


def test_numeric_settings_must_be_positive() -> None:
    assert any("positive number" in problem for problem in _validate(poll_seconds=0))
    assert any("positive number" in problem for problem in _validate(debounce_seconds=-1))
    assert any("positive number" in problem for problem in _validate(poll_seconds=True))
    assert _validate(poll_seconds=0.5, debounce_seconds=1.5) == []


def test_unknown_settings_are_reported() -> None:
    assert _validate(pattern="*.txt") == ['unknown setting "pattern"']


def test_emit_on_modify_must_be_a_boolean() -> None:
    assert any("true or false" in problem for problem in _validate(emit_on_modify="yes"))
