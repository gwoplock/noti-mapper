"""Secret loading and ``${secret:name}`` reference expansion.

Secrets live in ``/etc/noti-mapper/secrets.json``, deliberately outside the
``/etc/noti-mapper.d/`` glob so that they are not swept up by configuration
merging and can carry different permissions.

The design goal, worth stating plainly because it is what the rule buys you:
configuration files never contain secret material, which means they stay safe
to paste into a GitHub issue.
"""

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from noti_mapper.jsonloc import JsonObject, JsonParseError, JsonScalar, parse_file

DEFAULT_SECRETS_PATH: Path = Path("/etc/noti-mapper/secrets.json")

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
    that it carries different permissions.
    """
    if not path.exists():
        return empty_store(path)

    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SecretsError(
            f"{path} is mode {mode:04o}, which is readable by group or other. "
            f"Secrets must be mode 0600. Fix it with: chmod 0600 {path}"
        )

    try:
        document = parse_file(path)
    except JsonParseError as error:
        raise SecretsError(f"{path} is not valid JSON: {error.message}") from error

    if not isinstance(document, JsonObject):
        raise SecretsError(f"{path} must contain a JSON object mapping secret names to strings")

    values: dict[str, str] = {}
    for name, node in document.members.items():
        if SECRET_NAME_PATTERN.fullmatch(name) is None:
            raise SecretsError(
                f"{path}: secret name {name!r} contains characters outside " "[A-Za-z0-9_.-]"
            )
        if not isinstance(node, JsonScalar) or not isinstance(node.value, str):
            raise SecretsError(f"{path}: secret {name!r} must be a string")
        values[name] = node.value

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
