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

import unicodedata

MAX_NAME_LENGTH: int = 128

# Space, hyphen, underscore, period. Every other non-alphanumeric character is
# rejected. Note that this is U+0020 specifically: a non-breaking space is not
# an allowed character, because two names differing only by which space they
# contain is precisely the confusing bug report this is meant to prevent.
ALLOWED_PUNCTUATION: frozenset[str] = frozenset(" -_.")


class InvalidNameError(ValueError):
    """A name violates the naming rules."""


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
    if character in ALLOWED_PUNCTUATION:
        return True
    category = unicodedata.category(character)
    return category.startswith("L") or category.startswith("N")


def _describe_character(character: str) -> str:
    try:
        return f"{character!r} ({unicodedata.name(character)})"
    except ValueError:
        return f"{character!r} (U+{ord(character):04X})"


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
