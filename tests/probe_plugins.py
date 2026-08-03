"""Plugin source used by the end-to-end runtime tests.

These are written to disk as real plugin directories so that discovery,
instantiation, threading, and shutdown are exercised the way they will be in
production rather than mocked out. They signal through files because that is
the simplest thing a test can poke from the outside.
"""

from pathlib import Path

PROBE_INPUT_SOURCE = '''
import datetime
import threading
from collections.abc import Mapping
from pathlib import Path

from noti_mapper.plugin import InputPlugin, ObservedEvent, PluginHealth
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "probe-input"

POLL_SECONDS = 0.02


class ProbeInput(InputPlugin):
    """Emits an event whenever ``trigger_file`` appears, then removes it."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        if "trigger_file" not in settings:
            return ['"trigger_file" is required']
        return []

    def __init__(self, *, context, emit) -> None:
        super().__init__(context=context, emit=emit)
        self._stop = threading.Event()
        self._trigger = Path(str(context.settings["trigger_file"]))
        catch_up = context.settings.get("catch_up_file")
        self._catch_up = None if catch_up is None else Path(str(catch_up))

    def start(self) -> None:
        while not self._stop.wait(timeout=POLL_SECONDS):
            if not self._trigger.exists():
                continue
            payload = self._trigger.read_text(encoding="utf-8")
            self._trigger.unlink()
            self.emit(
                ObservedEvent(
                    occurred_at=self.context.clock.now(),
                    metadata={"payload": payload},
                )
            )

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK, detail="watching " + str(self._trigger))

    def catch_up(self, since):
        if self._catch_up is None or not self._catch_up.exists():
            return []
        stamp = datetime.datetime.fromisoformat(
            self._catch_up.read_text(encoding="utf-8").strip()
        )
        return [ObservedEvent(occurred_at=stamp, metadata={"payload": "catch-up"})]


INPUT_PLUGIN = ProbeInput
'''

PROBE_OUTPUT_SOURCE = '''
import datetime
import threading
from collections.abc import Mapping
from pathlib import Path

from noti_mapper.plugin import (
    OutputPlugin,
    OutputUpdate,
    PluginError,
    PluginHealth,
    RemoteBelief,
    RemoteState,
)
from noti_mapper.storage import HealthStatus

PLUGIN_NAME = "probe-output"

POLL_SECONDS = 0.02


class ProbeOutput(OutputPlugin):
    """Writes its state to a file and unlatches when ``unlatch_file`` appears."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        if "state_file" not in settings:
            return ['"state_file" is required']
        return []

    def __init__(self, *, context, request_unlatch) -> None:
        super().__init__(context=context, request_unlatch=request_unlatch)
        self._state_file = Path(str(context.settings["state_file"]))
        unlatch = context.settings.get("unlatch_file")
        self._unlatch_file = None if unlatch is None else Path(str(unlatch))
        belief = context.settings.get("belief_file")
        self._belief_file = None if belief is None else Path(str(belief))
        fail = context.settings.get("fail_file")
        self._fail_file = None if fail is None else Path(str(fail))
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._unlatch_file is None:
            return
        self._thread = threading.Thread(
            target=self._poll, name="probe-output-poll", daemon=True
        )
        self._thread.start()

    def _poll(self) -> None:
        while not self._stop.wait(timeout=POLL_SECONDS):
            if self._unlatch_file is not None and self._unlatch_file.exists():
                self._unlatch_file.unlink()
                self.request_unlatch("probe observed a clear")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def apply(self, update: OutputUpdate) -> None:
        if self._fail_file is not None and self._fail_file.exists():
            raise PluginError("probe-output was told to fail")
        self._state_file.write_text("true" if update.state else "false", encoding="utf-8")
        detail = Path(str(self._state_file) + ".detail")
        detail.write_text(update.summary(), encoding="utf-8")

    def query(self) -> RemoteState:
        if self._belief_file is None or not self._belief_file.exists():
            return RemoteState(belief=RemoteBelief.UNKNOWN)
        raw = self._belief_file.read_text(encoding="utf-8").strip()
        if raw == "active":
            return RemoteState(belief=RemoteBelief.ACTIVE)
        if raw == "cleared":
            return RemoteState(belief=RemoteBelief.CLEARED)
        if raw.startswith("cleared:"):
            return RemoteState(
                belief=RemoteBelief.CLEARED,
                cleared_at=datetime.datetime.fromisoformat(raw.split(":", 1)[1]),
            )
        return RemoteState(belief=RemoteBelief.UNKNOWN)

    def health(self) -> PluginHealth:
        return PluginHealth(status=HealthStatus.OK)


OUTPUT_PLUGIN = ProbeOutput
'''


def write_probe_plugins(directory: Path) -> Path:
    """Materialize the probe plugins into ``directory`` and return it."""
    for name, source in (
        ("probe_input", PROBE_INPUT_SOURCE),
        ("probe_output", PROBE_OUTPUT_SOURCE),
    ):
        plugin_directory = directory / name
        plugin_directory.mkdir(parents=True, exist_ok=True)
        (plugin_directory / "__init__.py").write_text(source, encoding="utf-8")
    return directory
