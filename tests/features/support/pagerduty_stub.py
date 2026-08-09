"""A stand-in for PagerDuty, so a scenario can watch an incident open and close.

The daemon's only outward-facing behaviour that a test can observe from a
distance is what it sends to an output's remote. This is that remote: it
records every Events API v2 call, answers the REST incident query from the
state those calls put it in, and can be told to fail so that a scenario can
watch a push retry rather than a state change being lost.

It is deliberately not a mock. The daemon talks to it over HTTP exactly as it
would talk to PagerDuty, which is what makes a scenario written against it
worth anything.
"""

import datetime
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

OPEN = "triggered"
RESOLVED = "resolved"


@dataclass
class Incident:
    """One incident, keyed by the dedup key the daemon chose."""

    dedup_key: str
    status: str = OPEN
    resolved_at: datetime.datetime | None = None
    summary: str = ""
    trigger_count: int = 0
    custom_details: dict[str, object] = field(default_factory=dict)


class PagerDutyStub:
    """An HTTP server that behaves enough like PagerDuty to be believed."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._lock = threading.Lock()
        self._incidents: dict[str, Incident] = {}
        self._events: list[dict[str, object]] = []
        self._fail_events_with: int | None = None

        handler = _handler_for(self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="pagerduty-stub",
            daemon=True,
        )

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10.0)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def base_url(self, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}"

    # -- what a scenario asks it ----------------------------------------------

    def incident(self, dedup_key: str) -> Incident | None:
        with self._lock:
            found = self._incidents.get(dedup_key)
            if found is None:
                return None
            return Incident(**vars(found))

    def is_open(self, dedup_key: str) -> bool:
        found = self.incident(dedup_key)
        return found is not None and found.status == OPEN

    def event_count(self, dedup_key: str, action: str) -> int:
        with self._lock:
            return len(
                [
                    event
                    for event in self._events
                    if event.get("dedup_key") == dedup_key and event.get("event_action") == action
                ]
            )

    # -- what a scenario does to it -------------------------------------------

    def resolve_externally(self, dedup_key: str, *, at: datetime.datetime) -> None:
        """Resolve an incident the way a human closing it in the app would.

        This is the whole point of the stub: it is how a scenario exercises the
        reverse channel without the daemon being involved in the decision.
        """
        with self._lock:
            incident = self._incidents.get(dedup_key)
            if incident is None:
                incident = Incident(dedup_key=dedup_key)
                self._incidents[dedup_key] = incident
            incident.status = RESOLVED
            incident.resolved_at = at

    def fail_events_with(self, status: int | None) -> None:
        """Make the Events API return ``status`` until told otherwise."""
        with self._lock:
            self._fail_events_with = status

    def reset(self) -> None:
        with self._lock:
            self._incidents.clear()
            self._events.clear()
            self._fail_events_with = None

    # -- what the handler calls -----------------------------------------------

    def _record_event(self, payload: dict[str, object]) -> int:
        with self._lock:
            if self._fail_events_with is not None:
                return self._fail_events_with

            self._events.append(payload)
            dedup_key = str(payload.get("dedup_key", ""))
            action = str(payload.get("event_action", ""))

            if action == "trigger":
                incident = self._incidents.setdefault(dedup_key, Incident(dedup_key=dedup_key))
                incident.status = OPEN
                incident.resolved_at = None
                incident.trigger_count += 1
                body = payload.get("payload")
                if isinstance(body, dict):
                    incident.summary = str(body.get("summary", ""))
                    details = body.get("custom_details")
                    if isinstance(details, dict):
                        incident.custom_details = dict(details)
            elif action == "resolve":
                existing = self._incidents.get(dedup_key)
                if existing is not None:
                    existing.status = RESOLVED
                    existing.resolved_at = datetime.datetime.now(tz=datetime.UTC)
            return 202

    def _incidents_for(self, dedup_key: str) -> list[dict[str, object]]:
        with self._lock:
            incident = self._incidents.get(dedup_key)
            if incident is None:
                return []
            body: dict[str, object] = {"status": incident.status}
            if incident.resolved_at is not None:
                body["resolved_at"] = incident.resolved_at.isoformat()
            return [body]


def _handler_for(stub: PagerDutyStub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) or b"{}"
            try:
                payload = json.loads(raw)
            except ValueError:
                self._send(400, {"error": "not json"})
                return
            if not isinstance(payload, dict):
                self._send(400, {"error": "not an object"})
                return
            status = stub._record_event(payload)  # noqa: SLF001
            self._send(status, {"status": "success" if status < 300 else "error"})

        def do_GET(self) -> None:  # noqa: N802
            dedup_key = ""
            _, _, query = self.path.partition("?")
            for pair in query.split("&"):
                key, _, value = pair.partition("=")
                if key == "incident_key":
                    dedup_key = _unquote(value)
            self._send(200, {"incidents": stub._incidents_for(dedup_key)})  # noqa: SLF001

        def _send(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

    return Handler


def _unquote(value: str) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(value)
