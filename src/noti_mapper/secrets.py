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

from noti_mapper.jsonfile import JsonFileError, read_object

DEFAULT_SECRETS_PATH: Path = Path("/etc/noti-mapper/secrets.json")

# Secret names are not object names -- they never appear in the rule graph --
# so they have their own, narrower character set.
SECRET_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")


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
        document = read_object(path)
    except JsonFileError as error:
        raise SecretsError(f"{error}") from error

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
