"""HomeKit output: exposes latch state as a Switch accessory.

Read this part first, because it is the misconception that makes people reject
this class of solution: **this adds one accessory to your existing Apple Home
setup.** It does not require migrating your home, bridging devices you already
own, or re-pairing anything. You add a switch, and that switch is the latch.

Library: HAP-python, which is what Home Assistant's HomeKit bridge is built on.

The switch is bidirectional, which is the whole point:

* The read characteristic returns core state, pushed on every change so a
  subscribed controller stays live.
* A write of ``false`` is an unlatch, taken through the normal path.
* A write of ``true`` is accepted rather than refused -- it is useful when
  testing that the accessory is reachable, and it is harmless, because only an
  input can set a latch. The characteristic is immediately re-asserted to core
  state, so the switch snaps back and the user can see that the daemon, not the
  controller, owns this value.

``query()`` returns UNKNOWN, and that is correct rather than lazy. HomeKit
controllers do not hold state independently of the accessory: while the daemon
is down the accessory is simply unreachable, so nobody can have cleared it and
there is nothing to report. Reconciliation falls back to persisted state, which
is exactly right here.

Pairing state persists under the state directory. Re-pairing on restart is a
defect, not a quirk.

Networking, which is also a packaging concern: HAP needs mDNS/Bonjour on the
same L2 segment as the Apple Home hub. That rules out aggressive systemd
network sandboxing and any container setup without host networking. Users with
VLAN'd IoT networks need an mDNS reflector.
"""

import logging
import threading
from collections.abc import Mapping
from typing import Any

from pyhap import util as pyhap_util
from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_SWITCH

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

PLUGIN_NAME = "homekit-output"

DEFAULT_PORT = 51826
PERSIST_FILENAME = "homekit.state"
PINCODE_KEY = "pincode"
STOP_TIMEOUT_SECONDS = 15.0

_OPTIONAL_KEYS = ("display_name", "port", "address", "manufacturer", "model")


class LatchSwitch(Accessory):
    """A HomeKit Switch whose On characteristic is the latch."""

    category = CATEGORY_SWITCH

    def __init__(self, *, driver: AccessoryDriver, display_name: str, plugin: Any) -> None:
        super().__init__(driver, display_name)
        self._plugin = plugin

        service = self.add_preload_service("Switch")
        self._on = service.configure_char("On", setter_callback=self._written)
        information = self.get_service("AccessoryInformation")
        information.configure_char("Manufacturer", value=plugin.manufacturer)
        information.configure_char("Model", value=plugin.model)
        information.configure_char("SerialNumber", value=plugin.context.instance_name)

    def set_state(self, state: bool) -> None:
        """Push a new value to subscribed controllers."""
        self._on.set_value(state)

    def current_state(self) -> bool:
        return bool(self._on.get_value())

    def _written(self, value: object) -> None:
        self._plugin.characteristic_written(bool(value))


class HomeKitOutput(OutputPlugin):
    """One Switch accessory per output instance."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        problems: list[str] = []
        for key in settings:
            if key not in _OPTIONAL_KEYS:
                problems.append(f'unknown setting "{key}"')

        for key in ("display_name", "address", "manufacturer", "model"):
            value = settings.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                problems.append(f'"{key}" must be a non-empty string')

        port = settings.get("port")
        if port is not None and (
            isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535
        ):
            problems.append('"port" must be an integer between 0 and 65535')

        return problems

    def __init__(self, *, context: PluginContext, request_unlatch: UnlatchCallback) -> None:
        super().__init__(context=context, request_unlatch=request_unlatch)
        settings = context.settings
        # The accessory name a user sees in the Home app. Defaults to the
        # instance name, which is what they already typed into the config.
        self._display_name = str(settings.get("display_name", context.instance_name))
        self._port = int(str(settings.get("port", DEFAULT_PORT)))
        self._address = settings.get("address")
        self.manufacturer = str(settings.get("manufacturer", "noti-mapper"))
        self.model = str(settings.get("model", "Latch Switch"))
        self._log = context.logger

        self._driver: AccessoryDriver | None = None
        self._accessory: LatchSwitch | None = None
        self._thread: threading.Thread | None = None
        self._persist_file = context.state_directory / PERSIST_FILENAME
        self._first_run = True
        self._state_lock = threading.Lock()
        self._status = HealthStatus.STARTING
        self._detail = "not yet started"
        self._desired = False

    def health(self) -> PluginHealth:
        with self._state_lock:
            return PluginHealth(status=self._status, detail=self._detail)

    def query(self) -> RemoteState:
        """HomeKit holds no state of its own; see the module docstring."""
        return RemoteState(belief=RemoteBelief.UNKNOWN)

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        with self._state_lock:
            self._status = status
            self._detail = detail
