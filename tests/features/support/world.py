"""The world a scenario runs in: a daemon, its configuration, and a fake remote.

Two backends, chosen by ``NOTI_BDD_BACKEND``:

``local``
    The daemon is a subprocess in a scratch directory. Fast, and the default,
    so ``behave`` needs nothing installed but Python.

``docker``
    The daemon is the container built from ``docker/Dockerfile``, driven
    through ``docker compose``. Slower, and the one that proves the packaging,
    the paths, and the service user rather than just the code.

The step definitions do not know which is running. Everything a scenario does
-- POST a webhook, drop a file, read the incident, restart the daemon, run a
CLI subcommand -- goes through this class, so a scenario written once is
evidence about both.
"""

import abc
import datetime
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.features.support.pagerduty_stub import PagerDutyStub

REPOSITORY = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPOSITORY / "docker" / "docker-compose.yml"

WEBHOOK_PORT = 9736
WEBHOOK_SECRET = "a-secret-of-adequate-length"
READY_TIMEOUT_SECONDS = 60.0
SETTLE_TIMEOUT_SECONDS = 30.0


class DaemonUnderTest(abc.ABC):
    """However the daemon happens to be running, this is what a step can ask of it.

    The three paths are attributes rather than methods because both backends
    hand the test side real host paths: the local one because it is the same
    filesystem, the container one because the workspace is bind-mounted.
    """

    config_directory: Path
    secrets_path: Path
    state_directory: Path

    @abc.abstractmethod
    def start(self) -> None:
        """Start it and return once it is accepting webhooks."""

    @abc.abstractmethod
    def stop(self) -> None:
        """Stop it, the way systemd would."""

    @abc.abstractmethod
    def reload(self) -> None:
        """Ask it to re-read its configuration."""

    @abc.abstractmethod
    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run a noti-mapper subcommand against the same state directory."""

    @abc.abstractmethod
    def webhook_url(self) -> str:
        """Where a scenario POSTs to make an input fire."""

    @abc.abstractmethod
    def watched_directory(self) -> Path:
        """A directory the file input watches, writable from the test side."""

    @abc.abstractmethod
    def pagerduty_base_url(self) -> str:
        """What the daemon should be configured to call. Not what the test calls."""

    @abc.abstractmethod
    def logs(self) -> str:
        """Whatever the daemon has said, for when a scenario fails."""

    @property
    def running(self) -> bool:
        return False


@dataclass
class World:
    """Everything a scenario touches, assembled by ``environment.py``."""

    daemon: DaemonUnderTest
    stub: PagerDutyStub
    workspace: Path
    responses: dict[str, object] = field(default_factory=dict)

    # -- inputs ---------------------------------------------------------------

    def post_webhook(self, *, body: str = "{}", token: str | None = WEBHOOK_SECRET) -> int:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Noti-Mapper-Token"] = token
        request = urllib.request.Request(
            self.daemon.webhook_url(),
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=10.0) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            with error:
                return int(error.code)

    def drop_file(self, name: str, *, contents: str = "delivered", age_hours: float = 0.0) -> Path:
        path = self.daemon.watched_directory() / name
        path.write_text(contents, encoding="utf-8")
        if age_hours:
            when = time.time() - age_hours * 3600
            os.utime(path, (when, when))
        return path

    # -- waiting --------------------------------------------------------------

    def eventually(self, predicate: Callable[[], bool], *, what: str) -> None:
        deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    # -- state ----------------------------------------------------------------

    def latch_is_set(self, rule: str) -> bool:
        """Read the latch through the CLI, which is what an operator would do."""
        status = self.daemon.cli("status")
        for line in status.stdout.splitlines():
            if f"{rule!r}" in line and ("SET" in line or "clear" in line):
                return "SET" in line
        return False

    def status_text(self) -> str:
        return self.daemon.cli("status").stdout


# -- the local backend --------------------------------------------------------


class LocalDaemon(DaemonUnderTest):
    """The daemon as a subprocess, in a scratch directory."""

    def __init__(self, *, workspace: Path, pagerduty_url: str) -> None:
        self._workspace = workspace
        self._pagerduty_url = pagerduty_url
        self._process: subprocess.Popen[str] | None = None
        self._log: object | None = None

        self.config_directory = workspace / "conf.d"
        self.state_directory = workspace / "state"
        self.secrets_path = workspace / "secrets.json"
        self.watched = workspace / "watched"
        for directory in (self.config_directory, self.state_directory, self.watched):
            directory.mkdir(parents=True, exist_ok=True)

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            return
        # To a file, not a pipe. A pipe nobody drains fills its buffer and then
        # blocks the daemon on its next log line, which looks exactly like the
        # daemon having hung -- and the daemon is what is under test.
        self._log = (self._workspace / "daemon.log").open("a", encoding="utf-8")
        self._process = subprocess.Popen(
            [str(REPOSITORY / ".venv" / "bin" / "noti-mapper"), *self._paths(), "run"],
            cwd=REPOSITORY,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_port(
            "127.0.0.1",
            WEBHOOK_PORT,
            timeout=READY_TIMEOUT_SECONDS,
            process=self._process,
            log=self.log_path,
        )

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10.0)
        self._process = None
        if self._log is not None:
            self._log.close()  # type: ignore[attr-defined]
            self._log = None
        _wait_for_port_closed("127.0.0.1", WEBHOOK_PORT, timeout=30.0)

    def reload(self) -> None:
        process = self._process
        assert process is not None, "the daemon is not running"
        process.send_signal(1)  # SIGHUP

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- what a step needs ----------------------------------------------------

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(REPOSITORY / ".venv" / "bin" / "noti-mapper"), *self._paths(), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
        )

    def webhook_url(self) -> str:
        return f"http://127.0.0.1:{WEBHOOK_PORT}/hook"

    def watched_directory(self) -> Path:
        return self.watched

    def pagerduty_base_url(self) -> str:
        return self._pagerduty_url

    @property
    def log_path(self) -> Path:
        return self._workspace / "daemon.log"

    def logs(self) -> str:
        if not self.log_path.exists():
            return "(the daemon has not written anything)"
        return "\n".join(self.log_path.read_text(encoding="utf-8").splitlines()[-60:])

    def _paths(self) -> list[str]:
        return [
            "--config-dir",
            str(self.config_directory),
            "--secrets",
            str(self.secrets_path),
            "--state-dir",
            str(self.state_directory),
            "--plugin-dir",
            str(REPOSITORY / "plugins"),
        ]


# -- the container backend ----------------------------------------------------


class ContainerDaemon(DaemonUnderTest):
    """The daemon as the packaged container, driven through docker compose.

    The workspace is bind-mounted, so configuration is written and watched
    files are dropped from the test side exactly as the local backend does.
    """

    def __init__(self, *, workspace: Path, pagerduty_url: str) -> None:
        self._workspace = workspace
        self._pagerduty_url = pagerduty_url

        self.config_directory = workspace / "conf.d"
        self.state_directory = workspace / "state"
        self.secrets_path = workspace / "secrets.json"
        self.watched = workspace / "watched"
        for directory in (self.config_directory, self.state_directory, self.watched):
            directory.mkdir(parents=True, exist_ok=True)
        # The image runs as the noti-mapper user, which will not share this
        # machine's uid; the bind mount has to be writable by it either way.
        self.state_directory.chmod(0o777)

    def _compose(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), *arguments],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            env={**os.environ, "NOTI_BDD_WORKSPACE": str(self._workspace)},
        )

    def start(self) -> None:
        result = self._compose("up", "-d", "--wait", "noti-mapper")
        if result.returncode != 0:
            raise AssertionError(f"docker compose up failed:\n{result.stdout}{result.stderr}")
        _wait_for_port("127.0.0.1", WEBHOOK_PORT, timeout=READY_TIMEOUT_SECONDS, process=None)

    def stop(self) -> None:
        self._compose("stop", "-t", "30", "noti-mapper")
        _wait_for_port_closed("127.0.0.1", WEBHOOK_PORT, timeout=30.0)

    def reload(self) -> None:
        self._compose("kill", "-s", "HUP", "noti-mapper")

    @property
    def running(self) -> bool:
        result = self._compose("ps", "--status", "running", "--quiet", "noti-mapper")
        return bool(result.stdout.strip())

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._compose("exec", "-T", "noti-mapper", "noti-mapper", *arguments)

    def webhook_url(self) -> str:
        return f"http://127.0.0.1:{WEBHOOK_PORT}/hook"

    def watched_directory(self) -> Path:
        return self.watched

    def pagerduty_base_url(self) -> str:
        # Inside the container the stub is reachable on the host gateway; the
        # compose file adds the alias.
        return self._pagerduty_url.replace("127.0.0.1", "host.docker.internal")

    def logs(self) -> str:
        return self._compose("logs", "--no-color", "--tail", "200", "noti-mapper").stdout

    def tear_down(self) -> None:
        self._compose("down", "-v", "-t", "10")


# -- assembling the world -----------------------------------------------------


def backend_name() -> str:
    return os.environ.get("NOTI_BDD_BACKEND", "local").strip().lower()


def make_daemon(*, workspace: Path, pagerduty_url: str) -> DaemonUnderTest:
    name = backend_name()
    if name == "docker":
        if shutil.which("docker") is None:
            raise AssertionError(
                "NOTI_BDD_BACKEND=docker but docker is not on PATH. "
                "Run with NOTI_BDD_BACKEND=local to use a subprocess instead."
            )
        return ContainerDaemon(workspace=workspace, pagerduty_url=pagerduty_url)
    if name != "local":
        raise AssertionError(f"unknown NOTI_BDD_BACKEND {name!r}; expected 'local' or 'docker'")
    return LocalDaemon(workspace=workspace, pagerduty_url=pagerduty_url)


# -- configuration a scenario starts from -------------------------------------


def write_configuration(daemon: DaemonUnderTest, *, rules: dict[str, dict[str, list[str]]]) -> None:
    """Write the porch configuration, with whatever rules a scenario wants.

    One webhook input, one file input, and two PagerDuty outputs pointed at the
    stub. That is enough surface for latching, coupling, retries, and
    reconciliation, and every part of it is observable from outside the daemon.
    """
    config_directory = daemon.config_directory
    secrets_path = daemon.secrets_path
    watched = daemon.watched_directory()
    base = daemon.pagerduty_base_url()

    instances: dict[str, object] = {
        "Porch Hook": {
            "plugin": "webhook-input",
            "config": {
                "secret": "${secret:webhook_secret}",
                "port": WEBHOOK_PORT,
                "bind": "0.0.0.0",
                "path": "/hook",
            },
        },
        "Porch Files": {
            "plugin": "file-input",
            "config": {
                "path": str(_container_path(daemon, watched)),
                "glob": "*.txt",
                "poll_seconds": 0.2,
                "debounce_seconds": 0.4,
            },
        },
        "Porch Pager": _pager("Porch Pager", base),
        "Hall Pager": _pager("Hall Pager", base),
    }

    (config_directory / "10-instances.json").write_text(
        json.dumps({"instances": instances}, indent=2), encoding="utf-8"
    )
    (config_directory / "20-rules.json").write_text(
        json.dumps({"rules": rules}, indent=2), encoding="utf-8"
    )
    (config_directory / "30-daemon.json").write_text(
        json.dumps({"daemon": {"retry_initial_seconds": 1, "retry_max_seconds": 4}}, indent=2),
        encoding="utf-8",
    )
    secrets_path.write_text(json.dumps({"webhook_secret": WEBHOOK_SECRET}), encoding="utf-8")
    secrets_path.chmod(0o600)


def _pager(name: str, base: str) -> dict[str, object]:
    return {
        "plugin": "pagerduty-output",
        "config": {
            "routing_key": "stub-routing-key",
            "api_token": "stub-api-token",
            "events_url": f"{base}/v2/enqueue",
            "api_url": base,
            "dedup_key": dedup_key_for(name),
            "poll_seconds": 5,
        },
    }


def dedup_key_for(instance: str) -> str:
    """The key the PagerDuty output uses, so a scenario can look the incident up."""
    return f"noti-mapper/{instance}"


def _container_path(daemon: DaemonUnderTest, host_path: Path) -> Path:
    """Translate a host path into what the daemon will see.

    The local backend sees the same filesystem. The container sees the bind
    mount, which compose puts at a fixed place.
    """
    if isinstance(daemon, ContainerDaemon):
        return Path("/workspace") / host_path.name
    return host_path


def start_stub() -> PagerDutyStub:
    stub = PagerDutyStub(host="0.0.0.0", port=0)
    stub.start()
    return stub


def now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


# -- small waits --------------------------------------------------------------


def _wait_for_port(
    host: str,
    port: int,
    *,
    timeout: float,
    process: subprocess.Popen[str] | None,
    log: Path | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            output = log.read_text(encoding="utf-8") if log and log.exists() else ""
            raise AssertionError(f"the daemon exited before it was ready:\n{output}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"nothing accepted a connection on {host}:{port} within {timeout:.0f}s")


def _wait_for_port_closed(host: str, port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((host, port)) != 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"{host}:{port} was still accepting connections after {timeout:.0f}s")
