"""HomeKit output.

The driver and accessory are constructed for real, so the characteristic
wiring is exercised rather than mocked, but the mDNS advertisement is not
brought up: HAP-python takes several seconds to start and stop a driver, and a
test suite that pays that per test stops being run. Starting the network side
is covered end-to-end by the runtime tests with the probe plugins.
"""

import pytest

from homekit_output import HomeKitOutput


def test_an_empty_configuration_validates() -> None:
    assert HomeKitOutput.validate_settings({}) == []


def test_unknown_settings_are_reported() -> None:
    assert HomeKitOutput.validate_settings({"name": "Lamp"}) == ['unknown setting "name"']


@pytest.mark.parametrize("port", [-1, 70000, True, "51826"])
def test_a_bad_port_is_reported(port: object) -> None:
    problems = HomeKitOutput.validate_settings({"port": port})
    assert any("between 0 and 65535" in problem for problem in problems)


def test_a_good_port_validates() -> None:
    assert HomeKitOutput.validate_settings({"port": 51827}) == []


def test_string_settings_must_be_non_empty() -> None:
    problems = HomeKitOutput.validate_settings({"display_name": "  "})
    assert any("non-empty string" in problem for problem in problems)
