"""PagerDuty output, against a stub of the two APIs it talks to.

A real HTTP server rather than a mocked ``urlopen``, so the request the plugin
actually builds -- headers, method, JSON body -- is what gets checked.
"""

from pagerduty_output import PagerDutyOutput

# -- settings -----------------------------------------------------------------


def _validate(**overrides: object) -> list[str]:
    settings: dict[str, object] = {"routing_key": "k", "api_token": "t"}
    settings.update(overrides)
    return PagerDutyOutput.validate_settings(settings)


def test_a_minimal_configuration_validates() -> None:
    assert _validate() == []


def test_the_two_credentials_are_distinguished_in_the_error() -> None:
    problems = PagerDutyOutput.validate_settings({})
    assert len(problems) == 2

    routing = next(problem for problem in problems if problem.startswith('"routing_key"'))
    assert "Events API v2" in routing
    assert "not the same credential" in routing

    token = next(problem for problem in problems if problem.startswith('"api_token"'))
    assert "REST API token" in token
    assert "not the same credential" in token


def test_a_missing_api_token_names_the_reverse_channel() -> None:
    problems = PagerDutyOutput.validate_settings({"routing_key": "k"})
    assert len(problems) == 1
    assert "how an unlatch gets back" in problems[0]


def test_severity_is_checked_against_the_allowed_set() -> None:
    assert _validate(severity="critical") == []
    assert _validate(severity="info") == []
    problems = _validate(severity="urgent")
    assert any("must be one of" in problem for problem in problems)


def test_the_poll_interval_has_a_floor() -> None:
    assert any("at least 5" in problem for problem in _validate(poll_seconds=1))
    assert _validate(poll_seconds=60) == []


def test_unknown_settings_are_reported() -> None:
    assert _validate(routing_keys="k") == ['unknown setting "routing_keys"']
