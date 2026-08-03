"""PagerDuty output, against a stub of the two APIs it talks to.

A real HTTP server rather than a mocked ``urlopen``, so the request the plugin
actually builds -- headers, method, JSON body -- is what gets checked.
"""

import datetime
import http.server
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from noti_mapper.clock import ManualClock
from noti_mapper.plugin import OutputUpdate, PluginError
from noti_mapper.storage import Database, database_path, initialize
from pagerduty_output import PagerDutyOutput
from tests.support import make_context

LATCHED_AT = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.UTC)
RESOLVED_AT = datetime.datetime(2026, 3, 1, 14, 30, tzinfo=datetime.UTC)


@dataclass
class StubState:
    """What the fake PagerDuty should say and what it has been told."""

    events: list[dict[str, object]] = field(default_factory=list)
    event_headers: list[dict[str, str]] = field(default_factory=list)
    incidents: list[dict[str, object]] = field(default_factory=list)
    incident_queries: list[str] = field(default_factory=list)
    incident_headers: list[dict[str, str]] = field(default_factory=list)
    events_status: int = 202
    incidents_status: int = 200
    incidents_body: str | None = None


class _Stub:
    def __init__(self) -> None:
        self.state = StubState()
        handler = _make_handler(self.state)
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    @property
    def base(self) -> str:
        address = self._server.server_address
        return f"http://{address[0]!s}:{address[1]!s}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10.0)


def _make_handler(state: StubState) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            state.events.append(body)
            state.event_headers.append(dict(self.headers))
            self._send(state.events_status, json.dumps({"status": "success"}))

        def do_GET(self) -> None:  # noqa: N802
            state.incident_queries.append(self.path)
            state.incident_headers.append(dict(self.headers))
            if state.incidents_body is not None:
                self._send(state.incidents_status, state.incidents_body)
                return
            self._send(state.incidents_status, json.dumps({"incidents": state.incidents}))

        def _send(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    return Handler


@dataclass
class Harness:
    plugin: PagerDutyOutput
    stub: _Stub
    unlatches: list[str]


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    database = Database(path=database_path(tmp_path))
    initialize(database)
    stub = _Stub()
    unlatches: list[str] = []
    plugin = PagerDutyOutput(
        context=make_context(
            instance_name="Porch Pager",
            database=database,
            clock=ManualClock(start=LATCHED_AT),
            settings={
                "routing_key": "R0UT1NGK3Y",
                "api_token": "AP1T0K3N",
                "events_url": f"{stub.base}/v2/enqueue",
                "api_url": stub.base,
                "poll_seconds": 5,
            },
        ),
        request_unlatch=unlatches.append,
    )
    try:
        yield Harness(plugin=plugin, stub=stub, unlatches=unlatches)
    finally:
        plugin.stop()
        stub.close()
        database.close()


def _update(state: bool = True, **overrides: object) -> OutputUpdate:
    defaults: dict[str, object] = {
        "state": state,
        "cause": "Porch Mail",
        "detail": "sender='ups.com' subject='Delivered: box'",
        "trigger_count": 1,
        "rules": ("Package On Porch",),
        "since": LATCHED_AT,
    }
    defaults.update(overrides)
    return OutputUpdate(**defaults)  # type: ignore[arg-type]


# -- the write direction ------------------------------------------------------


def test_setting_the_latch_triggers_an_incident(harness: Harness) -> None:
    harness.plugin.apply(_update())

    assert len(harness.stub.state.events) == 1
    event = harness.stub.state.events[0]
    assert event["event_action"] == "trigger"
    assert event["routing_key"] == "R0UT1NGK3Y"
    assert event["dedup_key"] == "noti-mapper/Porch Pager"

    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["severity"] == "warning"
    assert payload["source"] == "Porch Pager"
    assert "Delivered: box" in str(payload["summary"])
    assert payload["timestamp"] == LATCHED_AT.isoformat()


def test_the_payload_carries_the_event_metadata(harness: Harness) -> None:
    harness.plugin.apply(_update(trigger_count=3, rules=("Package On Porch", "Front Door")))

    payload = harness.stub.state.events[0]["payload"]
    assert isinstance(payload, dict)
    details = payload["custom_details"]
    assert isinstance(details, dict)
    assert details["caused_by"] == "Porch Mail"
    assert "Delivered: box" in str(details["event"])
    assert details["trigger_count"] == 3
    assert details["rules"] == "Package On Porch, Front Door"
    assert details["latched_since"] == LATCHED_AT.isoformat()


def test_a_repeated_trigger_renders_the_count(harness: Harness) -> None:
    harness.plugin.apply(_update(trigger_count=3))
    payload = harness.stub.state.events[0]["payload"]
    assert isinstance(payload, dict)
    assert "3 triggers" in str(payload["summary"])


def test_clearing_the_latch_resolves_the_incident(harness: Harness) -> None:
    harness.plugin.apply(OutputUpdate(state=False))

    event = harness.stub.state.events[0]
    assert event["event_action"] == "resolve"
    assert event["dedup_key"] == "noti-mapper/Porch Pager"
    assert "payload" not in event


def test_the_dedup_key_is_stable_across_pushes(harness: Harness) -> None:
    harness.plugin.apply(_update())
    harness.plugin.apply(_update())
    harness.plugin.apply(OutputUpdate(state=False))

    keys = {event["dedup_key"] for event in harness.stub.state.events}
    assert keys == {"noti-mapper/Porch Pager"}


def test_a_failed_push_raises_so_the_core_retries(harness: Harness) -> None:
    harness.stub.state.events_status = 503
    with pytest.raises(PluginError, match="503"):
        harness.plugin.apply(_update())


def test_an_unreachable_endpoint_raises(tmp_path: Path) -> None:
    database = Database(path=database_path(tmp_path / "other"))
    initialize(database)
    plugin = PagerDutyOutput(
        context=make_context(
            instance_name="Pager",
            database=database,
            settings={
                "routing_key": "k",
                "api_token": "t",
                # Port 1 on loopback is not listening.
                "events_url": "http://127.0.0.1:1/v2/enqueue",
                "api_url": "http://127.0.0.1:1",
            },
        ),
        request_unlatch=lambda _cause: None,
    )
    try:
        with pytest.raises(PluginError, match="unreachable"):
            plugin.apply(_update())
    finally:
        database.close()


def test_the_events_api_is_not_sent_the_rest_token(harness: Harness) -> None:
    harness.plugin.apply(_update())
    assert "Authorization" not in harness.stub.state.event_headers[0]


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
