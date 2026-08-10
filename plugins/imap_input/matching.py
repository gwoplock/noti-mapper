"""Deciding whether a message is one the user cares about.

Kept apart from the IMAP machinery, and free of any third-party import, so
that the matching rules can be tested against a corpus of real carrier subject
lines without a mail server anywhere in sight.

The rule is: the sender must be on the allowlist *and* the subject must match
one of the patterns. Both, not either. A carrier sends far more mail than
delivery notifications, and a subject pattern alone matches marketing mail from
anyone.

Either constraint may be left out, and leaving one out means "any". A mailbox
that a filter already feeds only delivery mail needs no allowlist, and an
address used for nothing else needs no subject patterns. This is an *and* over
the constraints that exist, so dropping one widens what matches -- and dropping
both matches every message in the folder. That last case is legal and
occasionally what someone wants; the plugin says so out loud at startup rather
than leaving it to be discovered.

Message bodies are never parsed. Carrier and notification HTML changes
constantly and it is not worth the fragility.
"""

import email.header
import email.utils
import re
from dataclasses import dataclass


def decode_subject(raw: str | None) -> str:
    """Decode an RFC 2047 encoded-word subject into ordinary text.

    Real delivery notifications arrive with subjects like
    ``=?UTF-8?B?RGVsaXZlcmVkOiB5b3VyIHBhY2thZ2U=?=``. Matching a regular
    expression against that undecoded is a bug that only shows up for users
    whose carrier uses non-ASCII, which is to say later, and confusingly.
    """
    if raw is None:
        return ""

    pieces: list[str] = []
    for payload, charset in email.header.decode_header(raw):
        if isinstance(payload, bytes):
            try:
                pieces.append(payload.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                pieces.append(payload.decode("utf-8", errors="replace"))
        else:
            pieces.append(payload)
    return "".join(pieces).strip()


def sender_address(raw: str | None) -> str:
    """Extract the bare address from a From header."""
    if raw is None:
        return ""
    _, address = email.utils.parseaddr(decode_subject(raw))
    return address.strip().lower()


@dataclass(frozen=True)
class MatchResult:
    """Why a message did or did not match, for the dry-run log."""

    matched: bool
    reason: str


class Criteria:
    """A sender allowlist and a set of subject patterns, either of which may be empty.

    An empty list is not "matches nothing", it is "does not constrain". See the
    module docstring for why.
    """

    def __init__(self, *, senders: list[str], subject_patterns: list[str]) -> None:
        self._senders = [sender.strip().lower() for sender in senders]
        self._patterns = [re.compile(pattern) for pattern in subject_patterns]
        self._pattern_sources = list(subject_patterns)

    @property
    def senders(self) -> list[str]:
        return list(self._senders)

    @property
    def subject_patterns(self) -> list[str]:
        return list(self._pattern_sources)

    @property
    def constrains_sender(self) -> bool:
        return bool(self._senders)

    @property
    def constrains_subject(self) -> bool:
        return bool(self._patterns)

    def sender_allowed(self, address: str) -> bool:
        """True when the address is, or is at, an allowlisted sender.

        An entry with an ``@`` is matched as a whole address. An entry without
        one is matched as a domain, including subdomains, so ``ups.com`` covers
        ``noreply@mail.ups.com`` but not ``ups.com.example.net``.

        With no allowlist every sender is allowed, including one that could not
        be parsed into an address at all. "No allowlist" has to mean no
        allowlist; a message is not worth dropping over a malformed From header
        when the user has asked for all of them.
        """
        if not self._senders:
            return True

        candidate = address.strip().lower()
        if not candidate:
            return False

        _, _, domain = candidate.partition("@")
        for allowed in self._senders:
            if "@" in allowed:
                if candidate == allowed:
                    return True
                continue
            if domain == allowed or domain.endswith("." + allowed):
                return True
        return False

    def subject_matches(self, subject: str) -> bool:
        if not self._patterns:
            return True
        return any(pattern.search(subject) is not None for pattern in self._patterns)

    def evaluate(self, *, sender: str, subject: str) -> MatchResult:
        if not self.sender_allowed(sender):
            return MatchResult(matched=False, reason=f"sender {sender!r} is not on the allowlist")
        if not self.subject_matches(subject):
            return MatchResult(matched=False, reason=f"subject {subject!r} matches no pattern")
        return MatchResult(matched=True, reason=self._match_reason(subject))

    def _match_reason(self, subject: str) -> str:
        """Why a message matched, phrased so the dry-run log is worth reading.

        Naming the unconstrained side is the point. "matched" against a
        configuration that constrains nothing tells the user only that mail
        arrived, and they have to go back to the config to learn why every
        message is matching.
        """
        if self.constrains_subject:
            return f"subject {subject!r} matched"
        if self.constrains_sender:
            return "sender is allowed and no subject pattern is configured"
        return "nothing is configured to match on, so every message matches"


def compile_problems(patterns: list[str]) -> list[str]:
    """Return a message for every subject pattern that is not a valid regex."""
    problems: list[str] = []
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as error:
            problems.append(f'"subject_patterns" entry {pattern!r} is not a valid regex: {error}')
    return problems
