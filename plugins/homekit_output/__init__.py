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
from pathlib import Path
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

from .pairing import setup_code_text, setup_uri, write_setup_code_file

PLUGIN_NAME = "homekit-output"

DEFAULT_PORT = 51826
PERSIST_FILENAME = "homekit.state"
SETUP_CODE_FILENAME = "setup-code.txt"
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
        self._state_lock = threading.Lock()
        self._status = HealthStatus.STARTING
        self._detail = "not yet started"
        self._desired = False

    # -- lifecycle ------------------------------------------------------------

    def build_accessory(self) -> None:
        """Construct the driver and the Switch, without advertising anything.

        Split from :meth:`start` because everything that can be got wrong about
        the accessory -- its name, its characteristics, where pairing state
        lives -- is decided here, while the part that follows brings up mDNS and
        binds a socket. Keeping them separate means the first can be exercised
        without the second.
        """
        persist_file = self.context.state_path(PERSIST_FILENAME)

        driver = AccessoryDriver(
            port=self._port,
            address=None if self._address is None else str(self._address),
            persist_file=str(persist_file),
            pincode=self._pincode(),
        )
        accessory = LatchSwitch(driver=driver, display_name=self._display_name, plugin=self)
        driver.add_accessory(accessory=accessory)

        self._driver = driver
        self._accessory = accessory
        self._persist_file = persist_file

    def paired(self) -> bool:
        """Whether a controller has completed pairing.

        Asked of the driver rather than inferred from the persist file. The
        file is written when the driver starts, pairing or no pairing, so its
        existence answers "has this ever run?" and not "is it paired?" -- and
        the two differ during exactly the window where the setup code still
        matters.
        """
        driver = self._driver
        if driver is None:
            return False
        return bool(driver.state.paired)

    def setup_code(self) -> str:
        """The setup code as a person would type it, or "" before it is known."""
        driver = self._driver
        if driver is None:
            return ""
        return str(driver.state.pincode.decode("utf-8"))

    def setup_uri(self) -> str:
        """The setup code as a scannable ``X-HM://`` URI, or "" before it is known."""
        driver = self._driver
        if driver is None:
            return ""
        return setup_uri(setup_code=self.setup_code(), setup_id=str(driver.state.setup_id))

    def record_setup_code(self) -> Path | None:
        """Write the setup code beside the pairing state, and return where.

        The journal is a bad place to keep something you need to read once,
        weeks after it was written: it rotates, and finding it means knowing to
        look for it. This file does not rotate.

        A failure to write is logged and swallowed. The accessory works
        perfectly well without this file, and refusing to serve because a
        convenience file could not be written would be the wrong trade.
        """
        if self._driver is None:
            return None

        path = self.context.state_path(SETUP_CODE_FILENAME)
        try:
            write_setup_code_file(
                path,
                setup_code_text(
                    instance_name=self.context.instance_name,
                    display_name=self._display_name,
                    setup_code=self.setup_code(),
                    uri=self.setup_uri(),
                    paired=self.paired(),
                    written_at=self.context.clock.now(),
                ),
            )
        except OSError as error:
            self._log.warning(
                "could not write the HomeKit setup code to %s: %s. The code is in "
                "this log and in `noti-mapper status`.",
                path,
                error,
                extra={"instance": self.context.instance_name},
            )
            return None
        return path

    def _pincode(self) -> bytes:
        """The setup code, generated once and kept.

        HAP-python persists the pairing itself but not the setup code, so a
        restart before the user has finished pairing would otherwise print a
        different code than the one in the journal a minute earlier.
        """
        stored = self.context.storage.get(PINCODE_KEY)
        if stored is not None:
            return stored.encode("ascii")
        generated: bytes = pyhap_util.generate_pincode()
        self.context.storage.set(PINCODE_KEY, generated.decode("ascii"))
        return generated

    def start(self) -> None:
        try:
            self.build_accessory()
        except Exception as error:
            self._set_health(HealthStatus.FAILED, f"{type(error).__name__}: {error}")
            self._log.error(
                "HomeKit accessory %r failed to start: %s",
                self._display_name,
                error,
                extra={"instance": self.context.instance_name},
                exc_info=True,
            )
            return

        driver = self._driver
        assert driver is not None
        self._thread = threading.Thread(
            target=driver.start, name=f"homekit-{self.context.instance_name}", daemon=True
        )
        self._thread.start()

        self._set_health(HealthStatus.OK, f"accessory {self._display_name!r} on port {self._port}")
        self._log.info(
            "HomeKit accessory %r listening on port %d; pairing state in %s",
            self._display_name,
            self._port,
            self._persist_file,
            extra={"instance": self.context.instance_name},
        )
        recorded = self.record_setup_code()
        if not self.paired():
            self._log.info(
                "not yet paired. Add the accessory in the Home app with setup code %s. "
                "This adds one accessory to your existing home; nothing you already "
                "own is affected. The code is also in %s.",
                self.setup_code(),
                recorded if recorded is not None else "`noti-mapper status`",
                extra={"instance": self.context.instance_name, "setup_code_file": str(recorded)},
            )

    def stop(self) -> None:
        driver = self._driver
        if driver is None:
            return

        if self._thread is not None and self._thread.is_alive():
            try:
                driver.stop()
            except Exception as error:
                self._log.warning(
                    "HomeKit driver did not stop cleanly: %s",
                    error,
                    extra={"instance": self.context.instance_name},
                )
            self._thread.join(timeout=STOP_TIMEOUT_SECONDS)
        else:
            # Built but never served -- start() failed, or the accessory was
            # only constructed. The event loop and executor the driver made at
            # construction still need releasing; nothing else will do it.
            _release(driver, self._log)

        self._driver = None
        self._accessory = None
        self._thread = None
        self._set_health(HealthStatus.STOPPED, "not running")

    def health(self) -> PluginHealth:
        """How the accessory is doing, including whether anyone has paired.

        The pairing state is worked out on each call rather than recorded at
        start, because pairing happens minutes or days later and a value
        captured at start would be wrong for exactly as long as it mattered.
        The core polls this on a timer, so ``noti-mapper status`` catches up on
        its own.
        """
        with self._state_lock:
            status = self._status
            detail = self._detail

        if status is not HealthStatus.OK:
            return PluginHealth(status=status, detail=detail)

        if self.paired():
            return PluginHealth(status=status, detail=f"{detail}; paired")

        # An accessory nobody has paired with cannot deliver a notification, so
        # it is not OK -- and this is the line the user will be looking at when
        # they wonder why nothing is happening. It carries the code.
        return PluginHealth(
            status=HealthStatus.DEGRADED,
            detail=f"{detail}; not paired -- setup code {self.setup_code()}",
        )

    # -- the write direction --------------------------------------------------

    def apply(self, update: OutputUpdate) -> None:
        accessory = self._accessory
        if accessory is None:
            raise PluginError("the HomeKit accessory is not running")

        with self._state_lock:
            self._desired = update.state
        accessory.set_state(update.state)
        self._log.debug(
            "HomeKit switch %r set to %s (%s)",
            self._display_name,
            update.state,
            update.summary(),
            extra={"instance": self.context.instance_name, "value": update.state},
        )

    # -- the reverse direction ------------------------------------------------

    def characteristic_written(self, value: bool) -> None:
        """A controller wrote the On characteristic.

        False is an unlatch. True is accepted and then corrected, because only
        an input can set a latch -- see the module docstring.
        """
        if not value:
            self._log.info(
                "HomeKit switch %r written false; requesting unlatch",
                self._display_name,
                extra={"instance": self.context.instance_name},
            )
            self.request_unlatch("HomeKit switch written false")
            return

        with self._state_lock:
            desired = self._desired
        self._log.info(
            "HomeKit switch %r written true; only an input can set a latch, so the "
            "switch is being returned to %s",
            self._display_name,
            desired,
            extra={"instance": self.context.instance_name},
        )
        accessory = self._accessory
        if accessory is not None:
            accessory.set_state(desired)

    def query(self) -> RemoteState:
        """HomeKit holds no state of its own; see the module docstring."""
        return RemoteState(belief=RemoteBelief.UNKNOWN)

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        with self._state_lock:
            self._status = status
            self._detail = detail


def _release(driver: AccessoryDriver, log: logging.Logger) -> None:
    """Give back the event loop and thread pool an unstarted driver is holding."""
    try:
        if driver.executor is not None:
            driver.executor.shutdown(wait=False)
        if not driver.loop.is_closed():
            driver.loop.close()
    except Exception as error:
        log.debug("releasing an unstarted HomeKit driver: %s", error)


OUTPUT_PLUGIN = HomeKitOutput
