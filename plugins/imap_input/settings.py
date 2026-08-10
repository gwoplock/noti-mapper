"""Reading and validating the IMAP reader's configuration block.

Separate from the plugin class so that validation runs during ``noti-mapper
validate`` without a mail server, and so the defaults are all visible in one
place.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .matching import Criteria, compile_problems

# Small hosts commonly drop an IDLE connection well before the 29 minutes
# RFC 2177 suggests, so the default re-issue interval is 25.
DEFAULT_IDLE_REFRESH_SECONDS = 1500
DEFAULT_POLL_SECONDS = 60
DEFAULT_PORT_TLS = 993
DEFAULT_PORT_PLAIN = 143

REQUIRED_KEYS = ("host", "username", "password")
OPTIONAL_KEYS = (
    "senders",
    "subject_patterns",
    "port",
    "ssl",
    "folder",
    "idle_refresh_seconds",
    "poll_seconds",
    "dry_run",
    "reconnect_initial_seconds",
    "reconnect_max_seconds",
    "catch_up_limit",
)


@dataclass(frozen=True)
class ImapSettings:
    """Everything the reader needs, validated."""

    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    folder: str
    criteria: Criteria
    idle_refresh_seconds: int
    poll_seconds: int
    dry_run: bool
    reconnect_initial_seconds: float
    reconnect_max_seconds: float
    catch_up_limit: int


def validate(settings: Mapping[str, object]) -> list[str]:
    """Return every problem with a configuration block."""
    problems: list[str] = []

    for key in settings:
        if key not in REQUIRED_KEYS and key not in OPTIONAL_KEYS:
            problems.append(f'unknown setting "{key}"')

    for key in REQUIRED_KEYS:
        if key not in settings:
            problems.append(f'"{key}" is required')

    problems.extend(_string_problems(settings, "host"))
    problems.extend(_string_problems(settings, "username"))
    problems.extend(_string_problems(settings, "password"))
    problems.extend(_string_problems(settings, "folder"))
    problems.extend(_string_list_problems(settings, "senders"))
    problems.extend(_string_list_problems(settings, "subject_patterns"))

    patterns = settings.get("subject_patterns")
    if isinstance(patterns, list):
        problems.extend(compile_problems([item for item in patterns if isinstance(item, str)]))

    problems.extend(_positive_int_problems(settings, "port"))
    problems.extend(_positive_int_problems(settings, "idle_refresh_seconds"))
    problems.extend(_positive_int_problems(settings, "poll_seconds"))
    problems.extend(_positive_int_problems(settings, "catch_up_limit"))
    problems.extend(_bool_problems(settings, "ssl"))
    problems.extend(_bool_problems(settings, "dry_run"))

    return problems


def build(settings: Mapping[str, object]) -> ImapSettings:
    """Turn a validated block into typed settings."""
    use_ssl = bool(settings.get("ssl", True))
    default_port = DEFAULT_PORT_TLS if use_ssl else DEFAULT_PORT_PLAIN

    senders = [str(item) for item in _as_list(settings.get("senders"))]
    patterns = [str(item) for item in _as_list(settings.get("subject_patterns"))]

    return ImapSettings(
        host=str(settings["host"]),
        port=int(str(settings.get("port", default_port))),
        use_ssl=use_ssl,
        username=str(settings["username"]),
        password=str(settings["password"]),
        folder=str(settings.get("folder", "INBOX")),
        criteria=Criteria(senders=senders, subject_patterns=patterns),
        idle_refresh_seconds=int(
            str(settings.get("idle_refresh_seconds", DEFAULT_IDLE_REFRESH_SECONDS))
        ),
        poll_seconds=int(str(settings.get("poll_seconds", DEFAULT_POLL_SECONDS))),
        dry_run=bool(settings.get("dry_run", False)),
        reconnect_initial_seconds=float(str(settings.get("reconnect_initial_seconds", 5))),
        reconnect_max_seconds=float(str(settings.get("reconnect_max_seconds", 300))),
        catch_up_limit=int(str(settings.get("catch_up_limit", 200))),
    )


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    return []


def _string_problems(settings: Mapping[str, object], key: str) -> list[str]:
    value = settings.get(key)
    if value is None:
        return []
    if not isinstance(value, str) or not value.strip():
        return [f'"{key}" must be a non-empty string']
    return []


def _string_list_problems(settings: Mapping[str, object], key: str) -> list[str]:
    """Absent is allowed and means "any". Present-but-empty is a mistake.

    Omitting the key and writing ``[]`` read as the same intent to a person and
    would have to mean the same thing, but ``[]`` also arrives by accident --
    an edit that removed the last entry, a template rendered with nothing to
    put in it. Since the effect of getting this wrong is a mailbox that latches
    on every message, the ambiguous spelling is refused and the unambiguous one
    is required.
    """
    value = settings.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        return [f'"{key}" must be an array of strings']
    if not value:
        return [f'"{key}" is empty; omit it entirely to match any value']
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [f'"{key}" entries must be non-empty strings']
    return []


def _positive_int_problems(settings: Mapping[str, object], key: str) -> list[str]:
    value = settings.get(key)
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return [f'"{key}" must be a positive integer']
    return []


def _bool_problems(settings: Mapping[str, object], key: str) -> list[str]:
    value = settings.get(key)
    if value is None:
        return []
    if not isinstance(value, bool):
        return [f'"{key}" must be true or false']
    return []
