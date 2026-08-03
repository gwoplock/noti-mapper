"""Webhook receiver: a POST to a per-instance path emits an event.

Exists as much to keep the plugin interface honest as to be useful. If writing
this against the base class had been awkward, the interface would have been
shaped around IMAP and would have needed fixing before v1.

Two deliberate constraints:

* Shared-secret authentication via a header is **required**. There is no
  anonymous mode. An endpoint that latches your alerting with no credential is
  not a feature.
* The default bind address is localhost. Exposing this to the internet is the
  user's decision and their responsibility, and the documentation says so
  plainly rather than pretending the daemon can make it safe.

There is no catch-up. A webhook sender that fired while the daemon was down
got a connection refused; nothing was recorded anywhere for this plugin to go
back and find. Senders that need delivery guarantees should retry.
"""

import datetime
import hmac
import http.server
import json
import threading
from collections.abc import Mapping
from typing import Any

from noti_mapper.plugin import (
    EmitCallback,
    InputPlugin,
    ObservedEvent,
    PluginContext,
    PluginHealth,
    clamp_metadata,
)
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "webhook-input"

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 9736
DEFAULT_HEADER = "X-Noti-Mapper-Token"
DEFAULT_PATH = "/"

# A webhook body is arbitrary; refuse anything that is obviously not a
# notification rather than reading it into memory.
MAX_BODY_BYTES = 64 * 1024

_REQUIRED_KEYS = ("secret",)
_OPTIONAL_KEYS = ("bind", "port", "path", "header", "max_body_bytes")


class WebhookInput(InputPlugin):
    """Serves one path on one port."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        problems: list[str] = []
        for key in settings:
            if key not in _REQUIRED_KEYS and key not in _OPTIONAL_KEYS:
                problems.append(f'unknown setting "{key}"')

        secret = settings.get("secret")
        if secret is None:
            problems.append(
                '"secret" is required; this plugin has no anonymous mode. Put the '
                "value in /etc/noti-mapper/secrets.json and reference it as "
                '"${secret:name}".'
            )
        elif not isinstance(secret, str) or len(secret) < 16:
            problems.append('"secret" must be a string of at least 16 characters')

        for key in ("bind", "path", "header"):
            value = settings.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                problems.append(f'"{key}" must be a non-empty string')

        path = settings.get("path")
        if isinstance(path, str) and not path.startswith("/"):
            problems.append('"path" must start with "/"')

        for key in ("port", "max_body_bytes"):
            value = settings.get(key)
            if value is None:
                continue
            minimum = 0 if key == "port" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                problems.append(f'"{key}" must be a positive integer')

        port = settings.get("port")
        if isinstance(port, int) and not isinstance(port, bool) and port > 65535:
            problems.append('"port" must be below 65536')

        return problems

    def __init__(self, *, context: PluginContext, emit: EmitCallback) -> None:
        super().__init__(context=context, emit=emit)
        settings = context.settings
        self._bind = str(settings.get("bind", DEFAULT_BIND))
        self._port = int(str(settings.get("port", DEFAULT_PORT)))
        self._path = str(settings.get("path", DEFAULT_PATH))
        self._header = str(settings.get("header", DEFAULT_HEADER))
        self._secret = str(settings["secret"])
        self._max_body = int(str(settings.get("max_body_bytes", MAX_BODY_BYTES)))
        self._log = context.logger

        self._server: http.server.ThreadingHTTPServer | None = None
        self._bound_port: int | None = None
        self._listening = threading.Event()
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._status = HealthStatus.STARTING
        self._detail = "not yet listening"
        self._accepted = 0
        self._rejected = 0

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        handler = _make_handler(self)
        try:
            server = http.server.ThreadingHTTPServer((self._bind, self._port), handler)
        except OSError as error:
            self._set_health(HealthStatus.FAILED, f"cannot listen on {self._address()}: {error}")
            self._log.error(
                "webhook receiver cannot listen on %s: %s",
                self._address(),
                error,
                extra={"instance": self.context.instance_name},
            )
            return

        self._server = server
        self._bound_port = int(server.server_address[1])
        self._listening.set()
        self._set_health(HealthStatus.OK, f"listening on {self._address()}{self._path}")
        self._log.info(
            "webhook receiver listening on http://%s%s",
            self._address(),
            self._path,
            extra={"instance": self.context.instance_name},
        )
        if self._bind not in {"127.0.0.1", "::1", "localhost"}:
            self._log.warning(
                "webhook receiver is bound to %s, which is not loopback. Exposing "
                "this endpoint beyond the local machine is your decision and your "
                "responsibility.",
                self._bind,
                extra={"instance": self.context.instance_name},
            )

        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()
            self._listening.clear()
            self._set_health(HealthStatus.STOPPED, "not listening")

    def stop(self) -> None:
        self._stop.set()
        server = self._server
        if server is not None:
            server.shutdown()

    def health(self) -> PluginHealth:
        with self._state_lock:
            detail = self._detail
            if self._status is HealthStatus.OK:
                detail = f"{detail}; {self._accepted} accepted, {self._rejected} rejected"
            return PluginHealth(status=self._status, detail=detail)

    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        """Nothing to catch up on: a webhook that arrived while we were down is gone."""
        del since
        return []

    # -- request handling -----------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    @property
    def max_body_bytes(self) -> int:
        return self._max_body

    def authorized(self, presented: str | None) -> bool:
        """Constant-time comparison, so the secret cannot be guessed byte by byte."""
        if presented is None:
            return False
        return hmac.compare_digest(presented, self._secret)

    def header_name(self) -> str:
        return self._header

    def accept(self, *, body: bytes, source: str) -> None:
        metadata = {
            "source": source,
            "body": body.decode("utf-8", errors="replace"),
        }
        parsed = _parse_json(body)
        if parsed is not None:
            for key, value in parsed.items():
                metadata[f"json.{key}"] = str(value)

        with self._state_lock:
            self._accepted += 1

        self._log.info(
            "webhook accepted from %s",
            source,
            extra={"instance": self.context.instance_name, "source": source},
        )
        self.emit(
            ObservedEvent(occurred_at=self.context.clock.now(), metadata=clamp_metadata(metadata))
        )

    def reject(self, *, reason: str, source: str) -> None:
        with self._state_lock:
            self._rejected += 1
        self._log.warning(
            "webhook rejected from %s: %s",
            source,
            reason,
            extra={"instance": self.context.instance_name, "source": source},
        )

    @property
    def bound_port(self) -> int | None:
        """The port actually being served, which differs from ``port`` when 0 was asked for."""
        return self._bound_port

    def wait_until_listening(self, timeout: float) -> bool:
        """Block until the socket is open. Returns False on timeout."""
        return self._listening.wait(timeout=timeout)

    def _address(self) -> str:
        if self._bound_port is not None:
            return f"{self._bind}:{self._bound_port}"
        return f"{self._bind}:{self._port}"

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        with self._state_lock:
            self._status = status
            self._detail = detail


def _parse_json(body: bytes) -> dict[str, object] | None:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): value for key, value in parsed.items()}


def _make_handler(plugin: WebhookInput) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a handler class bound to one plugin instance.

    ``http.server`` instantiates the handler per request and gives it no place
    to carry state, so the instance is closed over here. This is the one place
    in the codebase where a class is built at runtime, and it is because the
    standard library's interface leaves no alternative.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "noti-mapper"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server dictates this name
            source = self.client_address[0]

            if self.path != plugin.path:
                plugin.reject(reason=f"unknown path {self.path!r}", source=source)
                self._respond(404, "not found")
                return

            if not plugin.authorized(self.headers.get(plugin.header_name())):
                plugin.reject(reason="missing or incorrect shared secret", source=source)
                self._respond(401, "unauthorized")
                return

            length = self._content_length()
            if length is None:
                plugin.reject(reason="missing or malformed Content-Length", source=source)
                self._respond(411, "length required")
                return
            if length > plugin.max_body_bytes:
                plugin.reject(reason=f"body of {length} bytes is too large", source=source)
                self._respond(413, "payload too large")
                return

            body = self.rfile.read(length)
            plugin.accept(body=body, source=source)
            self._respond(204, "")

        def do_GET(self) -> None:  # noqa: N802 - http.server dictates this name
            plugin.reject(reason="GET is not accepted", source=self.client_address[0])
            self._respond(405, "method not allowed")

        def _content_length(self) -> int | None:
            raw = self.headers.get("Content-Length")
            if raw is None:
                return None
            try:
                length = int(raw)
            except ValueError:
                return None
            if length < 0:
                return None
            return length

        def _respond(self, status: int, message: str) -> None:
            payload = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # http.server logs to stderr directly; route it through the plugin's
            # logger so it lands in the journal like everything else.
            plugin.context.logger.debug(format, *args)

    return Handler


INPUT_PLUGIN = WebhookInput
