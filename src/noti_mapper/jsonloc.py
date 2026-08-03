"""A JSON parser that remembers where every value came from.

Configuration errors must be reported with a file and a line. The standard
library's ``json`` module discards position information as it parses, so this
module provides a small recursive-descent parser that keeps it.

The parser is deliberately strict where the standard library is lenient:

* Duplicate keys in one object are an error, not last-one-wins. Silent
  shadowing is miserable to debug and there is no use case for it here.
* ``NaN``, ``Infinity``, and ``-Infinity`` are rejected. They are not JSON.
* Trailing commas and comments are rejected. They are not JSON either.

Everything else follows RFC 8259, and the test suite checks the parsed values
against ``json.loads`` for agreement.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Location:
    """A position in a configuration file. Lines and columns are 1-based."""

    path: Path
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"


@dataclass(frozen=True)
class JsonScalar:
    """A string, number, boolean, or null."""

    location: Location
    value: str | int | float | bool | None


@dataclass(frozen=True)
class JsonArray:
    """A JSON array. ``location`` is the opening bracket."""

    location: Location
    elements: list["JsonNode"]


@dataclass(frozen=True)
class JsonObject:
    """A JSON object. ``location`` is the opening brace.

    ``key_locations`` holds the position of each key's opening quote, which is
    what an error message about a named member should point at.
    """

    location: Location
    members: dict[str, "JsonNode"]
    key_locations: dict[str, Location]


JsonNode = JsonScalar | JsonArray | JsonObject


class JsonParseError(Exception):
    """The document is not valid JSON."""

    def __init__(self, message: str, location: Location) -> None:
        super().__init__(f"{location}: {message}")
        self.message = message
        self.location = location


def to_plain(node: JsonNode) -> object:
    """Strip locations, returning ordinary Python objects.

    Used for plugin configuration blocks, which plugins consume as plain data.
    """
    if isinstance(node, JsonScalar):
        return node.value
    if isinstance(node, JsonArray):
        plain_elements: list[object] = []
        for element in node.elements:
            plain_elements.append(to_plain(element))
        return plain_elements
    plain_members: dict[str, object] = {}
    for key, value in node.members.items():
        plain_members[key] = to_plain(value)
    return plain_members
