"""Talking to systemd, without a dependency on python-systemd.

``Type=notify`` and ``WatchdogSec`` are both just datagrams on the socket named
by ``$NOTIFY_SOCKET``. Writing them directly is a few lines and removes a
packaging dependency that would otherwise have to be declared in the PKGBUILD
for no benefit.

Readiness is signalled after configuration validation and initial
reconciliation, not at process start. A daemon that says it is ready before it
knows what its latches are is lying.
"""

import logging
import os
import socket

NOTIFY_SOCKET_ENVIRONMENT = "NOTIFY_SOCKET"


class Notifier:
    """Sends systemd notifications, or does nothing when not under systemd."""

    def __init__(self, *, address: str | None = None, logger: logging.Logger | None = None) -> None:
        self._address = (
            address if address is not None else os.environ.get(NOTIFY_SOCKET_ENVIRONMENT)
        )
        self._log = logger if logger is not None else logging.getLogger(__name__)

    @property
    def enabled(self) -> bool:
        return bool(self._address)

    def ready(self, status: str = "") -> None:
        """Tell systemd the service has finished starting."""
        message = "READY=1"
        if status:
            message = f"{message}\nSTATUS={status}"
        self._send(message)

    def status(self, text: str) -> None:
        self._send(f"STATUS={text}")

    def reloading(self) -> None:
        self._send("RELOADING=1")

    def stopping(self) -> None:
        self._send("STOPPING=1")

    def watchdog(self) -> None:
        """Keep the watchdog happy for one more interval."""
        self._send("WATCHDOG=1")

    def _send(self, message: str) -> None:
        address = self._address
        if not address:
            return
        # A leading '@' means an abstract socket, which the kernel spells with
        # a leading NUL byte.
        if address.startswith("@"):
            address = "\0" + address[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify_socket:
                notify_socket.connect(address)
                notify_socket.sendall(message.encode("utf-8"))
        except OSError as error:
            self._log.debug("could not notify systemd: %s", error)
