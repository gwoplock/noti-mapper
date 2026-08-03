from webhook_input import WebhookInput

SECRET = "a-secret-of-adequate-length"


# -- settings -----------------------------------------------------------------


def _validate(**overrides: object) -> list[str]:
    settings: dict[str, object] = {"secret": SECRET}
    settings.update(overrides)
    return WebhookInput.validate_settings(settings)


def test_a_minimal_configuration_validates() -> None:
    assert _validate() == []


def test_the_secret_is_required_and_says_where_to_put_it() -> None:
    problems = WebhookInput.validate_settings({})
    assert len(problems) == 1
    assert "no anonymous mode" in problems[0]
    assert "${secret:name}" in problems[0]


def test_a_short_secret_is_refused() -> None:
    assert any("at least 16 characters" in problem for problem in _validate(secret="short"))


def test_the_path_must_be_absolute() -> None:
    assert any('must start with "/"' in problem for problem in _validate(path="hook"))
    assert _validate(path="/hook") == []


def test_port_bounds_are_checked() -> None:
    assert _validate(port=0) == []  # 0 means "pick a free port"
    assert _validate(port=8080) == []
    assert any("below 65536" in problem for problem in _validate(port=70000))
    assert any("positive integer" in problem for problem in _validate(port=-1))


def test_unknown_settings_are_reported() -> None:
    assert _validate(bindaddr="0.0.0.0") == ['unknown setting "bindaddr"']
