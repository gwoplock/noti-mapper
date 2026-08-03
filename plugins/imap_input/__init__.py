"""IMAP reader: watches a mailbox and emits an event when a message matches.

Uses ``imapclient`` for its IDLE support. Hand-rolling an IMAP client is the
single largest source of subtle bugs this project could have acquired.

Three things this plugin will not do, and they are promises rather than
oversights:

* The mailbox is opened read-only. ``\\Seen`` is never set, nothing is moved,
  nothing is deleted. The user's mail client behaviour is completely
  unaffected.
* Message bodies are never parsed. Only the sender and the decoded subject.
* In dry-run mode nothing is emitted at all. Every message is evaluated and
  logged with what would have happened, so a user can tune patterns against
  live mail for a week before arming it. That is the difference between a
  project people keep and one they uninstall after it fires on "Out for
  delivery" twice a day.

Cursor handling deserves its own note. The plugin persists ``UIDVALIDITY`` and
the highest UID it has processed. If ``UIDVALIDITY`` changes -- the server
rebuilt the mailbox -- UID comparisons from before are meaningless, so tracking
is invalidated, the change is logged loudly, and the cursor is re-baselined to
the current highest UID *without emitting*. A server-side mailbox rebuild must
not produce a storm of false events.
"""

import contextlib
import datetime
import threading
from collections.abc import Mapping, Sequence
from typing import Any

import imapclient

from noti_mapper.plugin import (
    EmitCallback,
    InputPlugin,
    ObservedEvent,
    PluginContext,
    PluginHealth,
    clamp_metadata,
)
from noti_mapper.storage import HealthStatus

from .matching import MatchResult, decode_subject, sender_address
from .settings import ImapSettings, build, validate

PLUGIN_NAME = "imap-input"

UIDVALIDITY_KEY = "uidvalidity"
LAST_UID_KEY = "last_uid"


class ImapInput(InputPlugin):
    """Watches one mailbox on one account."""

    @classmethod
    def validate_settings(cls, settings: Mapping[str, object]) -> list[str]:
        return validate(settings)

    def __init__(self, *, context: PluginContext, emit: EmitCallback) -> None:
        super().__init__(context=context, emit=emit)
        self._settings: ImapSettings = build(context.settings)
        self._log = context.logger
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._status = HealthStatus.STARTING
        self._detail = "not yet connected"

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> PluginHealth:
        with self._state_lock:
            return PluginHealth(status=self._status, detail=self._detail)

    def _connect(self) -> Any:
        client = imapclient.IMAPClient(
            host=self._settings.host,
            port=self._settings.port,
            ssl=self._settings.use_ssl,
            use_uid=True,
            timeout=60,
        )
        client.login(self._settings.username, self._settings.password)
        return client

    def _select(self, client: Any) -> None:
        # readonly is the promise: no \Seen, no moves, no deletes.
        response = client.select_folder(self._settings.folder, readonly=True)
        uidvalidity = int(response[b"UIDVALIDITY"])
        self._reconcile_uidvalidity(client=client, uidvalidity=uidvalidity)
        self._set_health(
            HealthStatus.OK, f"watching {self._settings.folder} on {self._settings.host}"
        )

    # -- messages -------------------------------------------------------------

    def _new_uids(self, client: Any) -> list[int]:
        last_uid = self._stored_int(LAST_UID_KEY)
        if last_uid is None:
            return []
        # The documented downtime-recovery search: everything above the cursor.
        found = client.search(["UID", f"{last_uid + 1}:*"])
        # A "N:*" search returns the highest UID even when it is below N, which
        # is a long-standing IMAP wart rather than a server bug.
        return sorted(uid for uid in (int(item) for item in found) if uid > last_uid)

    # -- the cursor -----------------------------------------------------------

    def _reconcile_uidvalidity(self, *, client: Any, uidvalidity: int) -> None:
        stored = self._stored_int(UIDVALIDITY_KEY)
        if stored == uidvalidity and self._stored_int(LAST_UID_KEY) is not None:
            return

        highest = _highest_uid(client)
        if stored is None:
            self._log.info(
                "baselining %s at UID %d; messages already in the mailbox are not events",
                self._settings.folder,
                highest,
                extra={"instance": self.context.instance_name, "uidvalidity": uidvalidity},
            )
        else:
            self._log.warning(
                "UIDVALIDITY for %s changed from %d to %d. The mailbox was rebuilt "
                "server-side, so every stored UID is meaningless. Re-baselining at "
                "UID %d without emitting; messages that arrived during the rebuild "
                "will not be seen.",
                self._settings.folder,
                stored,
                uidvalidity,
                highest,
                extra={
                    "instance": self.context.instance_name,
                    "old_uidvalidity": stored,
                    "new_uidvalidity": uidvalidity,
                },
            )

        self.context.storage.set(UIDVALIDITY_KEY, str(uidvalidity))
        self.context.storage.set(LAST_UID_KEY, str(highest))

    def _advance_cursor(self, uid: int) -> None:
        self.context.storage.set(LAST_UID_KEY, str(uid))

    def _stored_int(self, key: str) -> int | None:
        raw = self.context.storage.get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            self._log.warning(
                "stored %s is not a number (%r); treating it as unset",
                key,
                raw,
                extra={"instance": self.context.instance_name},
            )
            return None

    def _set_health(self, status: HealthStatus, detail: str) -> None:
        with self._state_lock:
            self._status = status
            self._detail = detail


def _highest_uid(client: Any) -> int:
    found = client.search(["ALL"])
    if not found:
        return 0
    return max(int(uid) for uid in found)


def _close_quietly(client: Any) -> None:
    """Hang up. A connection we are already abandoning cannot fail usefully."""
    try:
        client.logout()
    except Exception:
        with contextlib.suppress(Exception):
            client.shutdown()
