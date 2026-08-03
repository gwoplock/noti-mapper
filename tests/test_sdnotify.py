import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper.sdnotify import Notifier, watchdog_interval_seconds


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[tuple[socket.socket, str]]:
    address = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(address)
    server.settimeout(2.0)
    try:
        yield (server, address)
    finally:
        server.close()


def _received(server: socket.socket) -> str:
    return server.recv(4096).decode("utf-8")


def test_ready_sends_ready_one(listener: tuple[socket.socket, str]) -> None:
    server, address = listener
    Notifier(address=address).ready()
    assert _received(server) == "READY=1"


def test_ready_can_carry_a_status_line(listener: tuple[socket.socket, str]) -> None:
    server, address = listener
    Notifier(address=address).ready(status="3 rules, 1 latched")
    assert _received(server) == "READY=1\nSTATUS=3 rules, 1 latched"


def test_the_other_notifications(listener: tuple[socket.socket, str]) -> None:
    server, address = listener
    notifier = Notifier(address=address)

    notifier.status("reconciling")
    assert _received(server) == "STATUS=reconciling"

    notifier.reloading()
    assert _received(server) == "RELOADING=1"

    notifier.stopping()
    assert _received(server) == "STOPPING=1"

    notifier.watchdog()
    assert _received(server) == "WATCHDOG=1"


def test_without_a_socket_the_notifier_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = Notifier()
    assert notifier.enabled is False
    notifier.ready()  # must not raise


def test_a_dead_socket_is_not_fatal(tmp_path: Path) -> None:
    notifier = Notifier(address=str(tmp_path / "nothing-is-listening.sock"))
    assert notifier.enabled is True
    notifier.ready()  # must not raise


def test_the_socket_comes_from_the_environment(
    listener: tuple[socket.socket, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, address = listener
    monkeypatch.setenv("NOTIFY_SOCKET", address)
    Notifier().ready()
    assert _received(server) == "READY=1"


# -- watchdog interval --------------------------------------------------------


def test_the_watchdog_interval_is_half_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.delenv("WATCHDOG_PID", raising=False)
    assert watchdog_interval_seconds() == 15.0


def test_no_watchdog_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog_interval_seconds() is None


@pytest.mark.parametrize("value", ["", "0", "-1", "not a number"])
def test_a_nonsense_watchdog_interval_is_ignored(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", value)
    assert watchdog_interval_seconds() is None


def test_the_watchdog_is_ignored_when_it_is_meant_for_another_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "30000000")
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
    assert watchdog_interval_seconds() is None

    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    assert watchdog_interval_seconds() == 15.0
