"""IMAP reader: watches a mailbox and emits an event when a message matches.

Uses ``imapclient`` for its IDLE support. Hand-rolling an IMAP client is the
single largest source of subtle bugs this project could have acquired.

Three things this plugin will not do, and they are promises rather than
oversights:

* The mailbox is opened read-only. ``\\Seen`` is never set, nothing is moved,
  nothing is deleted. The user's mail client behaviour is completely
  unaffected.
* Message bodies are never parsed. Only the sender and the decoded subject.
* Neither the sender allowlist nor the subject patterns are required, and
  leaving one out means "any". Leaving out both makes every message in the
  folder an event, which is occasionally the point and is announced at startup
  either way.
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

# imapclient's idle_check takes a timeout; a short one keeps stop() responsive
# without re-issuing IDLE more often than the server needs.
IDLE_CHECK_SECONDS = 30


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
        self._announce_criteria()

    def _announce_criteria(self) -> None:
        """Say what this reader will match on, loudly when that is everything.

        Both criteria are optional and leaving one out widens what matches, so
        the config file no longer shows the whole story -- a reader with no
        subject patterns looks, on the page, much like one whose patterns are
        elsewhere. Saying it at startup means the journal answers "why did this
        fire?" without the config file in the other hand.

        Matching every message is a legitimate configuration for a mailbox that
        exists for one purpose. It is also what a truncated config file
        produces, and the two are indistinguishable from here, so it gets a
        warning rather than an info line.
        """
        criteria = self._settings.criteria
        if not criteria.constrains_sender and not criteria.constrains_subject:
            self._log.warning(
                "%s has neither a sender allowlist nor subject patterns, so every "
                'message in %s will be treated as an event. Set "senders" or '
                '"subject_patterns" if that is not what you meant.',
                self.context.instance_name,
                self._settings.folder,
                extra={"instance": self.context.instance_name, "folder": self._settings.folder},
            )
            return

        if not criteria.constrains_sender:
            self._log.info(
                "no sender allowlist; any sender matching a subject pattern is an event",
                extra={"instance": self.context.instance_name},
            )
        if not criteria.constrains_subject:
            self._log.info(
                "no subject patterns; any message from an allowlisted sender is an event",
                extra={"instance": self.context.instance_name},
            )

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Connect and watch until stopped, reconnecting with backoff."""
        delay = self._settings.reconnect_initial_seconds
        while not self._stop.is_set():
            try:
                self._watch_once()
                delay = self._settings.reconnect_initial_seconds
            except Exception as error:
                if self._stop.is_set():
                    return
                self._set_health(HealthStatus.FAILED, f"{type(error).__name__}: {error}")
                self._log.warning(
                    "IMAP connection to %s failed, retrying in %.0fs: %s",
                    self._settings.host,
                    delay,
                    error,
                    extra={"instance": self.context.instance_name, "host": self._settings.host},
                )
                if self._stop.wait(timeout=delay):
                    return
                delay = min(delay * 2, self._settings.reconnect_max_seconds)

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> PluginHealth:
        with self._state_lock:
            return PluginHealth(status=self._status, detail=self._detail)

    # -- downtime recovery ----------------------------------------------------

    def catch_up(self, since: datetime.datetime | None) -> list[ObservedEvent]:
        """Return matching messages that arrived while the daemon was stopped.

        ``since`` is not used to select messages: the persisted UID cursor is
        both more precise and immune to clock skew between here and the mail
        server. The message date is still what ends up on the event, because
        that is the timestamp reconciliation compares against an output's clear
        time.
        """
        del since

        client = self._connect()
        try:
            self._select(client)
            uids = self._new_uids(client)
            if not uids:
                return []
            if len(uids) > self._settings.catch_up_limit:
                self._log.warning(
                    "%d messages arrived during downtime; only the newest %d are examined",
                    len(uids),
                    self._settings.catch_up_limit,
                    extra={"instance": self.context.instance_name},
                )
                uids = uids[-self._settings.catch_up_limit :]

            events = self._evaluate(client=client, uids=uids, emit_now=False)
            self._advance_cursor(max(uids))
            return events
        finally:
            _close_quietly(client)

    # -- the watch loop -------------------------------------------------------

    def _watch_once(self) -> None:
        client = self._connect()
        try:
            self._select(client)
            self._process_new(client)
            if self._supports_idle(client):
                self._idle_loop(client)
            else:
                self._poll_loop(client)
        finally:
            _close_quietly(client)

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

    def _supports_idle(self, client: Any) -> bool:
        if client.has_capability("IDLE"):
            return True
        self._log.info(
            "%s does not advertise IDLE; polling every %ds instead",
            self._settings.host,
            self._settings.poll_seconds,
            extra={"instance": self.context.instance_name},
        )
        self._set_health(HealthStatus.DEGRADED, "server does not support IDLE; polling")
        return False

    def _idle_loop(self, client: Any) -> None:
        deadline = self.context.clock.monotonic() + self._settings.idle_refresh_seconds
        client.idle()
        try:
            while not self._stop.is_set():
                responses = client.idle_check(timeout=IDLE_CHECK_SECONDS)
                if responses:
                    client.idle_done()
                    self._process_new(client)
                    client.idle()
                    deadline = self.context.clock.monotonic() + self._settings.idle_refresh_seconds
                    continue
                if self.context.clock.monotonic() >= deadline:
                    # Re-issue IDLE before the server or an intermediate NAT
                    # decides the connection is idle enough to drop.
                    return
        finally:
            _idle_done_quietly(client)

    def _poll_loop(self, client: Any) -> None:
        while not self._stop.wait(timeout=self._settings.poll_seconds):
            client.noop()
            self._process_new(client)

    def _process_new(self, client: Any) -> None:
        uids = self._new_uids(client)
        if not uids:
            return
        self._evaluate(client=client, uids=uids, emit_now=True)
        self._advance_cursor(max(uids))

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

    def _evaluate(self, *, client: Any, uids: Sequence[int], emit_now: bool) -> list[ObservedEvent]:
        fetched = client.fetch(list(uids), ["ENVELOPE", "INTERNALDATE"])
        events: list[ObservedEvent] = []

        for uid in sorted(fetched):
            envelope = fetched[uid].get(b"ENVELOPE")
            if envelope is None:
                continue
            sender = _sender_of(envelope)
            subject = decode_subject(_decode(envelope.subject))
            occurred_at = _message_date(
                envelope=envelope,
                internal_date=fetched[uid].get(b"INTERNALDATE"),
                fallback=self.context.clock.now(),
            )

            result = self._settings.criteria.evaluate(sender=sender, subject=subject)
            self._log_evaluation(uid=uid, sender=sender, subject=subject, result=result)
            if not result.matched or self._settings.dry_run:
                continue

            event = ObservedEvent(
                occurred_at=occurred_at,
                metadata=clamp_metadata(
                    {
                        "sender": sender,
                        "subject": subject,
                        "date": occurred_at.isoformat(),
                        "folder": self._settings.folder,
                        "uid": str(uid),
                    }
                ),
            )
            events.append(event)
            if emit_now:
                self.emit(event)

        return events

    def _log_evaluation(self, *, uid: int, sender: str, subject: str, result: MatchResult) -> None:
        if self._settings.dry_run:
            verdict = "would emit" if result.matched else "would ignore"
            self._log.info(
                "dry run: %s UID %d from %s -- %s",
                verdict,
                uid,
                sender,
                result.reason,
                extra={
                    "instance": self.context.instance_name,
                    "uid": uid,
                    "sender": sender,
                    "subject": subject,
                    "would_emit": result.matched,
                },
            )
            return

        if result.matched:
            self._log.info(
                "matched UID %d from %s: %s",
                uid,
                sender,
                subject,
                extra={"instance": self.context.instance_name, "uid": uid, "sender": sender},
            )
        else:
            self._log.debug(
                "ignored UID %d: %s",
                uid,
                result.reason,
                extra={"instance": self.context.instance_name, "uid": uid},
            )

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


def _decode(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _sender_of(envelope: Any) -> str:
    addresses = getattr(envelope, "from_", None) or ()
    for address in addresses:
        mailbox = _decode(address.mailbox) or ""
        host = _decode(address.host) or ""
        if mailbox and host:
            return f"{mailbox}@{host}".lower()
    return sender_address(_decode(getattr(envelope, "sender", None)))


def _message_date(
    *, envelope: Any, internal_date: object, fallback: datetime.datetime
) -> datetime.datetime:
    """The time the message says it was sent, preferred over when we saw it."""
    for candidate in (getattr(envelope, "date", None), internal_date):
        if isinstance(candidate, datetime.datetime):
            if candidate.tzinfo is None:
                return candidate.replace(tzinfo=datetime.UTC)
            return candidate.astimezone(datetime.UTC)
    return fallback


def _close_quietly(client: Any) -> None:
    """Hang up. A connection we are already abandoning cannot fail usefully."""
    try:
        client.logout()
    except Exception:
        with contextlib.suppress(Exception):
            client.shutdown()


def _idle_done_quietly(client: Any) -> None:
    with contextlib.suppress(Exception):
        client.idle_done()


# catch_up() lets imapclient's own exceptions propagate rather than wrapping
# them in PluginError. The core treats both identically: it logs the failure,
# falls back to persisted state rather than blocking startup, and retries in
# the background.
INPUT_PLUGIN = ImapInput
