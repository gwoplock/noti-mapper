"""Reading a JSON configuration file, strictly, on the standard library.

``json.loads`` does the parsing. Two hooks tighten it where the defaults are
looser than a configuration file wants:

* ``object_pairs_hook`` sees every key/value pair including repeats, which is
  how duplicate keys become an error rather than last-one-wins. Silent
  shadowing is miserable to debug and there is no use case for it here.
* ``parse_constant`` fires for ``NaN``, ``Infinity``, and ``-Infinity``, which
  the standard library accepts and JSON does not.

Trailing commas and comments are already rejected by ``json.loads``.

What is deliberately *not* here is the position of each value. The standard
library reports a line and column for a syntax error and nothing for anything
else, and recovering per-value positions means keeping a second parser in step
with the first one forever. Configuration errors carry a path through the
document instead -- see :class:`noti_mapper.config.ConfigPath`.
"""

import json
from collections.abc import Mapping
from pathlib import Path


class JsonFileError(Exception):
    """A file is not usable JSON.

    ``detail`` is the problem on its own and ``path`` is the file it is in, so
    a caller that already prints the file name can use one without repeating
    the other.
    """

    def __init__(self, *, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def parse(*, text: str, path: Path) -> object:
    """Parse JSON strictly. Raises :class:`JsonFileError` with the file named."""
    try:
        return json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as error:
        raise JsonFileError(
            path=path, detail=f"line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    except ValueError as error:
        # Raised by the hooks above, which json.loads passes through unchanged.
        raise JsonFileError(path=path, detail=str(error)) from error


def read_object(path: Path) -> Mapping[str, object]:
    """Read a file that must contain a JSON object at the top level."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise JsonFileError(path=path, detail=f"cannot be read: {error.strerror}") from error

    document = parse(text=text, path=path)
    if not isinstance(document, dict):
        raise JsonFileError(path=path, detail="the top level must be an object")
    return document
