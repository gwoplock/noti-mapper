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
