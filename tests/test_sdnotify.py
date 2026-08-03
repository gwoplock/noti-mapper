import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from noti_mapper.sdnotify import Notifier


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
