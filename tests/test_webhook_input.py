import http.client
import json
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from noti_mapper.clock import ManualClock
from noti_mapper.plugin import ObservedEvent
from noti_mapper.storage import Database, HealthStatus, database_path, initialize
from tests.support import make_context
from webhook_input import WebhookInput

SECRET = "a-secret-of-adequate-length"


class Harness:
    def __init__(self, plugin: WebhookInput, events: list[ObservedEvent]) -> None:
        self.plugin = plugin
        self.events = events
        self._thread = threading.Thread(target=plugin.start, name="webhook-test", daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.plugin.wait_until_listening(timeout=10.0), "the server never bound"

    def stop(self) -> None:
        self.plugin.stop()
        self._thread.join(timeout=10.0)

    def post(
        self,
        *,
        path: str = "/hook",
        body: bytes = b"{}",
        headers: Mapping[str, str] | None = None,
        method: str = "POST",
    ) -> int:
        port = self.plugin.bound_port
        assert port is not None
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10.0)
        try:
            sent = {"X-Noti-Mapper-Token": SECRET}
            if headers is not None:
                sent = dict(headers)
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    database = Database(path=database_path(tmp_path))
    initialize(database)
    events: list[ObservedEvent] = []
    plugin = WebhookInput(
        context=make_context(
            instance_name="Hook",
            database=database,
            clock=ManualClock(),
            settings={"secret": SECRET, "port": 0, "path": "/hook"},
        ),
        emit=events.append,
    )
    harness = Harness(plugin, events)
    harness.start()
    try:
        yield harness
    finally:
        harness.stop()
        database.close()


# -- the happy path -----------------------------------------------------------


def test_an_authenticated_post_emits_an_event(harness: Harness) -> None:
    assert harness.post(body=b'{"carrier": "ups"}') == 204

    assert len(harness.events) == 1
    metadata = harness.events[0].metadata
    assert metadata["body"] == '{"carrier": "ups"}'
    assert metadata["json.carrier"] == "ups"
    assert metadata["source"] == "127.0.0.1"


def test_a_non_json_body_is_still_captured(harness: Harness) -> None:
    assert harness.post(body=b"just some text") == 204
    assert harness.events[0].metadata["body"] == "just some text"
    assert not any(key.startswith("json.") for key in harness.events[0].metadata)


def test_an_empty_body_is_accepted(harness: Harness) -> None:
    assert harness.post(body=b"") == 204
    assert len(harness.events) == 1


# -- authentication is not optional -------------------------------------------


def test_a_request_with_no_token_is_refused(harness: Harness) -> None:
    assert harness.post(headers={}) == 401
    assert harness.events == []


def test_a_request_with_the_wrong_token_is_refused(harness: Harness) -> None:
    assert harness.post(headers={"X-Noti-Mapper-Token": "wrong"}) == 401
    assert harness.events == []


def test_a_token_that_is_a_prefix_of_the_secret_is_refused(harness: Harness) -> None:
    assert harness.post(headers={"X-Noti-Mapper-Token": SECRET[:-1]}) == 401
    assert harness.events == []


def test_the_wrong_path_is_a_404(harness: Harness) -> None:
    assert harness.post(path="/somewhere-else") == 404
    assert harness.events == []


def test_get_is_not_accepted(harness: Harness) -> None:
    assert harness.post(method="GET", body=b"") == 405
    assert harness.events == []


def test_an_oversized_body_is_refused(tmp_path: Path) -> None:
    database = Database(path=database_path(tmp_path))
    initialize(database)
    events: list[ObservedEvent] = []
    plugin = WebhookInput(
        context=make_context(
            instance_name="Hook",
            database=database,
            settings={"secret": SECRET, "port": 0, "path": "/hook", "max_body_bytes": 16},
        ),
        emit=events.append,
    )
    harness = Harness(plugin, events)
    harness.start()
    try:
        assert harness.post(body=b"x" * 1000) == 413
        assert events == []
        assert harness.post(body=b"small") == 204
    finally:
        harness.stop()
        database.close()


# -- health and lifecycle -----------------------------------------------------


def test_health_reports_listening_and_counts(harness: Harness) -> None:
    harness.post()
    harness.post(headers={})

    health = harness.plugin.health()
    assert health.status is HealthStatus.OK
    assert "1 accepted" in health.detail
    assert "1 rejected" in health.detail


def test_there_is_nothing_to_catch_up_on(harness: Harness) -> None:
    assert harness.plugin.catch_up(None) == []


def test_a_port_already_in_use_fails_health_rather_than_the_daemon(
    harness: Harness, tmp_path: Path
) -> None:
    database = Database(path=database_path(tmp_path / "second"))
    initialize(database)
    port = harness.plugin.bound_port
    assert port is not None

    clash = WebhookInput(
        context=make_context(
            instance_name="Clash",
            database=database,
            settings={"secret": SECRET, "port": port, "path": "/hook"},
        ),
        emit=lambda _event: None,
    )
    try:
        clash.start()  # returns rather than raising
        health = clash.health()
        assert health.status is HealthStatus.FAILED
        assert "cannot listen" in health.detail
    finally:
        clash.stop()
        database.close()


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


def test_json_metadata_is_flattened_one_level(harness: Harness) -> None:
    body = json.dumps({"event": "delivered", "count": 2, "nested": {"a": 1}}).encode("utf-8")
    harness.post(body=body)

    metadata = harness.events[0].metadata
    assert metadata["json.event"] == "delivered"
    assert metadata["json.count"] == "2"
    assert metadata["json.nested"] == "{'a': 1}"
