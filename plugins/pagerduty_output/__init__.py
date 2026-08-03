"""PagerDuty output: renders latch state as an incident, treats resolution as an unlatch.

Events API v2 for the write direction -- ``trigger`` on set, ``resolve`` on
clear -- and the REST API, polled, for the reverse direction.

Polling rather than inbound webhooks, deliberately. A webhook needs a publicly
reachable endpoint: a tunnel, TLS, and attack surface, for a daemon that will
typically run on a home LAN. Polling is outbound-only and resumes cleanly after
an ISP outage.

The ``dedup_key`` is stable per output instance. Repeat triggers collapse into
the open incident, which is correct: the alert is already outstanding. It also
makes ``trigger`` idempotent, which reconciliation depends on -- step 5 pushes
every output whether or not the daemon believes it is already correct.

Two distinct credentials are required, and they are not interchangeable:

* ``routing_key`` -- an Events API v2 integration key, from the service's
  integrations tab. Writes incidents.
* ``api_token`` -- a REST API token, from the user or account API access page.
  Reads incident status.

Configuring one and not the other is a predictable first-run stumble, so the
validation message says which is which rather than "missing field".
"""

import datetime
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from noti_mapper.plugin import (
    OutputPlugin,
    OutputUpdate,
    PluginContext,
    PluginError,
    PluginHealth,
    RemoteBelief,
    RemoteState,
    UnlatchCallback,
)
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "pagerduty-output"

DEFAULT_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"
DEFAULT_API_URL = "https://api.pagerduty.com"
DEFAULT_POLL_SECONDS = 45
DEFAULT_SEVERITY = "warning"
REQUEST_TIMEOUT_SECONDS = 20.0

VALID_SEVERITIES = ("critical", "error", "warning", "info")

# PagerDuty considers these statuses "the incident is still outstanding".
OPEN_STATUSES = ("triggered", "acknowledged")

_REQUIRED_KEYS = ("routing_key", "api_token")
_OPTIONAL_KEYS = (
    "dedup_key",
    "severity",
    "summary",
    "source",
    "component",
    "group",
    "class",
    "poll_seconds",
    "events_url",
    "api_url",
)


class PagerDutyOutput(OutputPlugin):
    """One PagerDuty incident, kept in step with one output instance."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        problems: list[str] = []
        for key in settings:
            if key not in _REQUIRED_KEYS and key not in _OPTIONAL_KEYS:
                problems.append(f'unknown setting "{key}"')

        if "routing_key" not in settings:
            problems.append(
                '"routing_key" is required. This is the Events API v2 integration '
                "key from the PagerDuty service's Integrations tab, and it is not "
                'the same credential as "api_token". It is what writes incidents.'
            )
        elif not isinstance(settings["routing_key"], str) or not settings["routing_key"].strip():
            problems.append('"routing_key" must be a non-empty string')

        if "api_token" not in settings:
            problems.append(
                '"api_token" is required. This is a REST API token from PagerDuty\'s '
                'API Access page, and it is not the same credential as "routing_key". '
                "It is what reads incident status, which is how an unlatch gets back "
                "here."
            )
        elif not isinstance(settings["api_token"], str) or not settings["api_token"].strip():
            problems.append('"api_token" must be a non-empty string')

        severity = settings.get("severity")
        if severity is not None and severity not in VALID_SEVERITIES:
            listed = ", ".join(VALID_SEVERITIES)
            problems.append(f'"severity" must be one of: {listed}')

        for key in ("dedup_key", "summary", "source", "component", "group", "class"):
            value = settings.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                problems.append(f'"{key}" must be a non-empty string')

        poll = settings.get("poll_seconds")
        if poll is not None and (isinstance(poll, bool) or not isinstance(poll, int) or poll < 5):
            problems.append('"poll_seconds" must be an integer of at least 5')

        return problems

    def __init__(self, *, context: PluginContext, request_unlatch: UnlatchCallback) -> None:
        super().__init__(context=context, request_unlatch=request_unlatch)
        settings = context.settings
        self._routing_key = str(settings["routing_key"])
        self._api_token = str(settings["api_token"])
        self._dedup_key = str(settings.get("dedup_key", f"noti-mapper/{context.instance_name}"))
        self._severity = str(settings.get("severity", DEFAULT_SEVERITY))
        self._summary = str(settings.get("summary", f"noti-mapper: {context.instance_name}"))
        self._source = str(settings.get("source", context.instance_name))
        self._component = _optional_str(settings.get("component"))
        self._group = _optional_str(settings.get("group"))
        self._class = _optional_str(settings.get("class"))
        self._poll_seconds = int(str(settings.get("poll_seconds", DEFAULT_POLL_SECONDS)))
        self._events_url = str(settings.get("events_url", DEFAULT_EVENTS_URL))
        self._api_url = str(settings.get("api_url", DEFAULT_API_URL)).rstrip("/")
        self._log = context.logger

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._status = HealthStatus.STARTING
        self._detail = "not yet polled"

    @property
    def dedup_key(self) -> str:
        return self._dedup_key

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"pagerduty-poll-{self.context.instance_name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 5.0)

    def health(self) -> PluginHealth:
        with self._state_lock:
            return PluginHealth(status=self._status, detail=self._detail)

    # -- the write direction --------------------------------------------------

    def apply(self, update: OutputUpdate) -> None:
        action = "trigger" if update.state else "resolve"
        payload: dict[str, object] = {
            "routing_key": self._routing_key,
            "event_action": action,
            "dedup_key": self._dedup_key,
        }
        if update.state:
            payload["payload"] = self._event_payload(update)

        self._log.info(
            "%s PagerDuty incident %s",
            action,
            self._dedup_key,
            extra={"instance": self.context.instance_name, "dedup_key": self._dedup_key},
        )
        self._post(self._events_url, payload)

    def _event_payload(self, update: OutputUpdate) -> dict[str, object]:
        details: dict[str, object] = {
            "instance": self.context.instance_name,
            "rules": ", ".join(update.rules),
            "trigger_count": update.trigger_count,
        }
        if update.cause:
            details["caused_by"] = update.cause
        if update.detail:
            details["event"] = update.detail
        if update.since is not None:
            details["latched_since"] = update.since.isoformat()

        payload: dict[str, object] = {
            "summary": f"{self._summary}: {update.summary()}"[:1024],
            "severity": self._severity,
            "source": self._source,
            "custom_details": details,
        }
        if update.since is not None:
            payload["timestamp"] = update.since.isoformat()
        if self._component is not None:
            payload["component"] = self._component
        if self._group is not None:
            payload["group"] = self._group
        if self._class is not None:
            payload["class"] = self._class
        return payload

    # -- the reverse direction ------------------------------------------------

    def query(self) -> RemoteState:
        """Ask the REST API whether the incident is still open.

        This is the unlatch path's source of truth, and it is also what
        reconciliation reads. A resolved incident reports CLEARED along with the
        time PagerDuty resolved it, which is what lets an event that arrived
        after the resolve win.
        """
        try:
            incidents = self._fetch_incidents()
        except PluginError as error:
            self._set_health(HealthStatus.DEGRADED, str(error))
            return RemoteState(belief=RemoteBelief.UNKNOWN)

        self._set_health(HealthStatus.OK, f"polling every {self._poll_seconds}s")

        if not incidents:
            # No incident has ever existed for this dedup key, so there is
            # nothing to report either way. Saying CLEARED here would clear a
            # latch on the strength of never having alerted.
            return RemoteState(belief=RemoteBelief.UNKNOWN)

        resolved_at: datetime.datetime | None = None
        for incident in incidents:
            status = str(incident.get("status", ""))
            if status in OPEN_STATUSES:
                return RemoteState(belief=RemoteBelief.ACTIVE)
            moment = _parse_time(incident.get("resolved_at")) or _parse_time(
                incident.get("last_status_change_at")
            )
            if moment is not None and (resolved_at is None or moment > resolved_at):
                resolved_at = moment

        return RemoteState(belief=RemoteBelief.CLEARED, cleared_at=resolved_at)

    def poll_once(self) -> None:
        """One polling pass: read incident status, and unlatch if it resolved.

        An unlatch against already-cleared state is a no-op in the core, so a
        poll that observes a clear this daemon itself caused does no harm. That
        is the loop-prevention contract, and it is why this can be unconditional
        rather than trying to remember which clears were ours.
        """
        state = self.query()
        if state.belief is not RemoteBelief.CLEARED:
            return
        self.request_unlatch("PagerDuty incident resolved")

    def _poll_loop(self) -> None:
        while not self._stop.wait(timeout=self._poll_seconds):
            self.poll_once()

    def _fetch_incidents(self) -> list[dict[str, object]]:
        query = urlencode(
            [("incident_key", self._dedup_key), ("limit", "10"), ("sort_by", "created_at:desc")]
        )
        url = f"{self._api_url}/incidents?{query}"
        body = self._get(url)
        incidents = body.get("incidents")
        if not isinstance(incidents, list):
            return []
        result: list[dict[str, object]] = []
        for item in incidents:
            if isinstance(item, dict):
                result.append({str(key): value for key, value in item.items()})
        return result

    # -- HTTP -----------------------------------------------------------------

    def _post(self, url: str, payload: Mapping[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        return self._send(request)

    def _get(self, url: str) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/vnd.pagerduty+json;version=2",
                "Authorization": f"Token token={self._api_token}",
            },
        )
        return self._send(request)

    def _send(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read().decode("utf-8", errors="replace")[:500]
            raise PluginError(
                f"PagerDuty returned {error.code} for {request.method} {request.full_url}: {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise PluginError(f"PagerDuty is unreachable: {error}") from error

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as error:
            raise PluginError(f"PagerDuty returned a body that is not JSON: {error}") from error
        if not isinstance(parsed, dict):
            return {}
        return {str(key): value for key, value in parsed.items()}

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        with self._state_lock:
            self._status = status
            self._detail = detail


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_time(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


OUTPUT_PLUGIN = PagerDutyOutput
