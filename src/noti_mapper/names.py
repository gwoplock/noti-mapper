"""Name validation and uniqueness tracking.

Every identifier in every config file is a "name". There are no numeric IDs, no
UUIDs, and no auto-generated keys anywhere in the config surface or the CLI.
Names are what the user typed, and they appear verbatim in logs, ``status``
output, and error messages.

The rules, in one place so nothing else has to reimplement them:

* Allowed characters are letters, digits, spaces, hyphens, underscores, and
  periods. Everything else is rejected -- particularly quotes, slashes, and
  control characters, since names flow into CLI arguments and log lines.
* Leading and trailing whitespace is stripped on load; interior whitespace is
  preserved.
* Maximum length is 128 characters, measured after stripping.
* Names are stored case-sensitively but uniqueness is checked
  case-insensitively, so ``Porch Mail`` and ``porch mail`` in the same
  namespace is a config error rather than two objects.
* Instance names and rule names occupy separate namespaces. A rule may share a
  name with an instance without conflict.
"""

import enum
import unicodedata
from dataclasses import dataclass

MAX_NAME_LENGTH: int = 128

# Space, hyphen, underscore, period. Every other non-alphanumeric character is
# rejected. Note that this is U+0020 specifically: a non-breaking space is not
# an allowed character, because two names differing only by which space they
# contain is precisely the confusing bug report this is meant to prevent.
ALLOWED_PUNCTUATION: frozenset[str] = frozenset(" -_.")


class Namespace(enum.Enum):
    """Names live in one of two independent namespaces."""

    INSTANCE = "instance"
    RULE = "rule"


class InvalidNameError(ValueError):
    """A name violates the naming rules."""


@dataclass(frozen=True)
class RegisteredName:
    """A name that has been accepted into a registry, and where it came from."""

    name: str
    namespace: Namespace
    origin: str


def normalize_name(raw: str) -> str:
    """Strip leading and trailing whitespace. Interior whitespace is preserved."""
    return raw.strip()


def uniqueness_key(name: str) -> str:
    """Return the key used for case-insensitive uniqueness comparison.

    Unicode is normalized to NFC first so that a precomposed and a decomposed
    spelling of the same name collide rather than coexisting.
    """
    return unicodedata.normalize("NFC", name).casefold()


def _character_is_allowed(character: str) -> bool:
    """True for letters, digits, and the four allowed punctuation characters.

    ``str.isalnum()`` is the documented way to ask this. It is defined against
    the Unicode character database and moves with the standard library's
    Unicode version. Testing ``unicodedata.category()`` for an "L" or "N"
    prefix would be reimplementing it by hand against two-letter category codes
    -- and would quietly accept anything else that ever starts with those
    letters.
    """
    if character in ALLOWED_PUNCTUATION:
        return True
    return character.isalnum()


def _describe_character(character: str) -> str:
    """Render one character for an error message.

    The character on its own is no use when it is invisible, which is exactly
    when someone needs the error, so this pairs its repr with what it actually
    is. Most characters have a Unicode name, and "NO-BREAK SPACE" is precisely
    what the reader needs to see.

    Control characters have no Unicode name, so they fall back to the code
    point written the way the Unicode standard writes it: "U+" followed by at
    least four uppercase hexadecimal digits, zero-padded.
    """
    try:
        return f"{character!r} ({unicodedata.name(character)})"
    except ValueError:
        code_point = format(ord(character), "04X")
        return f"{character!r} (U+{code_point})"


def find_name_problems(raw: str) -> list[str]:
    """Return every problem with a name, as human-readable sentences.

    An empty list means the name is valid. This returns all problems rather
    than raising on the first one so that config validation can report
    everything wrong with a file in a single pass.
    """
    problems: list[str] = []
    name = normalize_name(raw)

    if not name:
        problems.append("name is empty")
        return problems

    if len(name) > MAX_NAME_LENGTH:
        problems.append(f"name is {len(name)} characters long; the maximum is {MAX_NAME_LENGTH}")

    rejected: list[str] = []
    for character in name:
        if not _character_is_allowed(character) and character not in rejected:
            rejected.append(character)

    if rejected:
        listed = ", ".join(_describe_character(character) for character in rejected)
        problems.append(
            f"name contains disallowed characters: {listed}. "
            "Allowed characters are letters, digits, spaces, hyphens, "
            "underscores, and periods."
        )

    return problems


def validate_name(raw: str) -> str:
    """Normalize and validate a name, returning it. Raises on any problem.

    Use this at boundaries that handle one name at a time -- CLI arguments, for
    example. Config validation uses :func:`find_name_problems` instead so that
    it can accumulate errors.
    """
    problems = find_name_problems(raw)
    if problems:
        raise InvalidNameError(f"{raw!r}: " + "; ".join(problems))
    return normalize_name(raw)


class NameRegistry:
    """Tracks which names have been used, per namespace.

    Uniqueness is enforced case-insensitively. The registry also supports
    looking a name up case-insensitively, which is used to turn "no such
    instance" into "no such instance; did you mean ...".
    """

    def __init__(self) -> None:
        self._by_namespace: dict[Namespace, dict[str, RegisteredName]] = {
            namespace: {} for namespace in Namespace
        }

    def find_conflict(self, *, name: str, namespace: Namespace) -> RegisteredName | None:
        """Return the already-registered name this one would collide with, if any."""
        return self._by_namespace[namespace].get(uniqueness_key(normalize_name(name)))

    def register(self, *, name: str, namespace: Namespace, origin: str) -> RegisteredName:
        """Add a name to the registry.

        The caller is expected to have checked :meth:`find_conflict` already and
        reported a useful error; registering a conflicting name raises.
        """
        normalized = normalize_name(name)
        key = uniqueness_key(normalized)
        existing = self._by_namespace[namespace].get(key)
        if existing is not None:
            raise InvalidNameError(
                f"{namespace.value} name {normalized!r} conflicts with "
                f"{existing.name!r} defined at {existing.origin}"
            )
        registered = RegisteredName(name=normalized, namespace=namespace, origin=origin)
        self._by_namespace[namespace][key] = registered
        return registered

    def resolve(self, *, name: str, namespace: Namespace) -> RegisteredName | None:
        """Return the registered entry whose name matches exactly, if any."""
        entry = self.find_conflict(name=name, namespace=namespace)
        if entry is None:
            return None
        if entry.name != normalize_name(name):
            return None
        return entry

    def suggest(self, *, name: str, namespace: Namespace) -> str | None:
        """Return a registered name differing only by case, for error messages."""
        entry = self.find_conflict(name=name, namespace=namespace)
        if entry is None or entry.name == normalize_name(name):
            return None
        return entry.name

    def names(self, namespace: Namespace) -> list[str]:
        """Return every registered name in a namespace, sorted for stable output."""
        entries = self._by_namespace[namespace].values()
        return sorted(entry.name for entry in entries)
