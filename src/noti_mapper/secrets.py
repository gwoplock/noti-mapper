"""Secret loading and ``${secret:name}`` reference expansion.

Secrets live in ``/etc/noti-mapper/secrets.json``, deliberately outside the
``/etc/noti-mapper.d/`` glob so that they are not swept up by configuration
merging and can carry different permissions.

The design goal, worth stating plainly because it is what the rule buys you:
configuration files never contain secret material, which means they stay safe
to paste into a GitHub issue.
"""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.jsonfile import JsonFileError, parse

DEFAULT_SECRETS_PATH: Path = Path("/etc/noti-mapper/secrets.json")

# Any access at all by "other", and write access by the group. Group *read* is
# deliberately allowed, because the arrangement it permits is the better one:
# root owns the file, the daemon's group reads it, and the daemon -- the thing
# on the network, and so the thing that might be compromised -- cannot rewrite
# its own credentials. Requiring 0600 forced the file to be owned by the daemon
# user, which hands it exactly that write access.
#
# The group is the administrator's to choose. This can tell that a mode is
# wrong; it cannot tell that a group is too broad.
FORBIDDEN_MODE_BITS = 0o027

# Secret names are not object names -- they never appear in the rule graph --
# so they have their own, narrower character set.
SECRET_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
SECRET_REFERENCE_PATTERN = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")


class SecretsError(Exception):
    """The secrets file exists but cannot be used."""


@dataclass(frozen=True)
class SecretStore:
    """The loaded secrets, and where they came from."""

    path: Path
    values: dict[str, str]

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def names(self) -> list[str]:
        return sorted(self.values)


def empty_store(path: Path = DEFAULT_SECRETS_PATH) -> SecretStore:
    """A store with nothing in it, for when no secrets file is present."""
    return SecretStore(path=path, values={})


def load_secrets(path: Path) -> SecretStore:
    """Load the secrets file.

    A missing file is not an error -- most installations have no secrets. A
    file that any other user can read is an error, and it is reported loudly
    with the offending mode, because the whole point of the separate file is
    that it carries different permissions. See :data:`FORBIDDEN_MODE_BITS` for
    exactly which arrangements are allowed and why.

    A file that exists but cannot be read is a third case, and it must not be
    quietly folded into the first. ``Path.exists()`` answers False when the
    file is there but a directory above it is not searchable, so testing it
    turns "you are not allowed to read this" into "you have no secrets" -- and
    the user is then told every secret is undefined while looking straight at
    the file that defines them all.

    The file is opened once and the mode taken from that descriptor rather than
    from a second look at the name. This is the one file in the system where it
    is worth being sure that the thing whose permissions were approved is the
    thing whose bytes were read. It is why this does not call
    :func:`noti_mapper.jsonfile.read_object`, which opens by name.
    """
    try:
        with path.open("rb") as handle:
            status = os.fstat(handle.fileno())
            raw = handle.read()
    except FileNotFoundError:
        return empty_store(path)
    except OSError as error:
        raise SecretsError(
            f"{path}: cannot be read: {error.strerror}. The file is there, so this "
            "is a permissions problem rather than a missing file: reaching it needs "
            "search permission on every directory above it as well as read "
            "permission on the file itself."
        ) from error

    mode = stat.S_IMODE(status.st_mode)
    if mode & FORBIDDEN_MODE_BITS:
        raise SecretsError(
            f"{path} is mode {mode:04o}, which is readable by other users or "
            "writable by its group. Use 0600 owned by the user the daemon runs "
            "as, or 0640 owned by root with the daemon's group -- the second is "
            f"better, since then the daemon cannot rewrite its own credentials. "
            f"Fix it with: chmod 0640 {path}"
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SecretsError(f"{path}: is not valid UTF-8: {error}") from error

    try:
        document = parse(text=text, path=path)
    except JsonFileError as error:
        raise SecretsError(f"{error}") from error

    if not isinstance(document, dict):
        raise SecretsError(f"{path}: the top level must be an object")

    values: dict[str, str] = {}
    for name, value in document.items():
        if SECRET_NAME_PATTERN.fullmatch(name) is None:
            raise SecretsError(
                f"{path}: secret name {name!r} contains characters outside [A-Za-z0-9_.-]"
            )
        if not isinstance(value, str):
            raise SecretsError(f"{path}: secret {name!r} must be a string")
        values[name] = value

    return SecretStore(path=path, values=values)


@dataclass(frozen=True)
class Substitution:
    """The result of expanding secret references in one string."""

    text: str
    missing: tuple[str, ...]


def substitute(*, text: str, store: SecretStore) -> Substitution:
    """Expand every ``${secret:name}`` reference in ``text``.

    References to secrets that are not in the store are left in place and
    reported in ``missing``; the caller turns those into config errors, since a
    missing secret is a validation failure rather than a runtime one.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = store.get(name)
        if value is None:
            if name not in missing:
                missing.append(name)
            return match.group(0)
        return value

    expanded = SECRET_REFERENCE_PATTERN.sub(replace, text)
    return Substitution(text=expanded, missing=tuple(missing))


def references_in(text: str) -> list[str]:
    """Return the secret names referenced by a string, in order of appearance."""
    found: list[str] = []
    for match in SECRET_REFERENCE_PATTERN.finditer(text):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found
