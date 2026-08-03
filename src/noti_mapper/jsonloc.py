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

import bisect
import re
from dataclasses import dataclass
from pathlib import Path

# Configuration files are hand-written and shallow. A limit well above anything
# a human would type keeps a pathological file from exhausting the C stack.
MAX_DEPTH: int = 64

_NUMBER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

_SIMPLE_ESCAPES: dict[str, str] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

_HIGH_SURROGATE_FIRST = 0xD800
_HIGH_SURROGATE_LAST = 0xDBFF
_LOW_SURROGATE_FIRST = 0xDC00
_LOW_SURROGATE_LAST = 0xDFFF


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


def parse(*, text: str, path: Path) -> JsonNode:
    """Parse a JSON document, returning a tree annotated with locations."""
    parser = _Parser(text=text, path=path)
    return parser.parse_document()


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


def _compute_line_starts(text: str) -> list[int]:
    starts = [0]
    index = text.find("\n")
    while index != -1:
        starts.append(index + 1)
        index = text.find("\n", index + 1)
    return starts


class _Parser:
    """Recursive-descent JSON parser.

    Written out longhand rather than built on the standard library's scanner
    because the whole point is the position bookkeeping the standard library
    throws away.
    """

    def __init__(self, *, text: str, path: Path) -> None:
        self._text = text
        self._path = path
        self._index = 0
        self._depth = 0
        self._line_starts = _compute_line_starts(text)

    # -- position bookkeeping -------------------------------------------------

    def _location_at(self, index: int) -> Location:
        line = bisect.bisect_right(self._line_starts, index)
        column = index - self._line_starts[line - 1] + 1
        return Location(path=self._path, line=line, column=column)

    def _here(self) -> Location:
        return self._location_at(min(self._index, max(len(self._text) - 1, 0)))

    def _error(self, message: str) -> JsonParseError:
        return JsonParseError(message, self._here())

    # -- character helpers ----------------------------------------------------

    def _at_end(self) -> bool:
        return self._index >= len(self._text)

    def _peek(self) -> str:
        if self._at_end():
            return ""
        return self._text[self._index]

    def _skip_whitespace(self) -> None:
        while not self._at_end() and self._text[self._index] in " \t\n\r":
            self._index += 1

    def _expect(self, character: str) -> None:
        if self._peek() != character:
            found = self._peek() or "end of file"
            raise self._error(f"expected {character!r} but found {found!r}")
        self._index += 1

    # -- grammar --------------------------------------------------------------

    def parse_document(self) -> JsonNode:
        self._skip_whitespace()
        if self._at_end():
            raise self._error("file is empty")
        node = self._parse_value()
        self._skip_whitespace()
        if not self._at_end():
            raise self._error("unexpected data after the top-level value")
        return node

    def _parse_value(self) -> JsonNode:
        if self._depth >= MAX_DEPTH:
            raise self._error(f"nesting deeper than {MAX_DEPTH} levels")

        character = self._peek()
        if character == "{":
            return self._parse_object()
        if character == "[":
            return self._parse_array()
        if character == '"':
            start = self._index
            return JsonScalar(location=self._location_at(start), value=self._parse_string())
        if character == "t":
            return self._parse_literal(text="true", value=True)
        if character == "f":
            return self._parse_literal(text="false", value=False)
        if character == "n":
            return self._parse_literal(text="null", value=None)
        if character == "-" or character.isdigit():
            return self._parse_number()
        if self._at_end():
            raise self._error("unexpected end of file where a value was expected")
        raise self._error(f"unexpected character {character!r} where a value was expected")

    def _parse_literal(self, *, text: str, value: bool | None) -> JsonScalar:
        start = self._index
        if not self._text.startswith(text, start):
            raise self._error(f"expected {text}")
        self._index += len(text)
        return JsonScalar(location=self._location_at(start), value=value)

    def _parse_number(self) -> JsonScalar:
        start = self._index
        match = _NUMBER_PATTERN.match(self._text, start)
        if match is None:
            raise self._error("malformed number")
        self._index = match.end()
        literal = match.group()
        location = self._location_at(start)
        if "." in literal or "e" in literal or "E" in literal:
            return JsonScalar(location=location, value=float(literal))
        return JsonScalar(location=location, value=int(literal))

    def _parse_array(self) -> JsonArray:
        start = self._index
        self._expect("[")
        self._depth += 1
        elements: list[JsonNode] = []

        self._skip_whitespace()
        if self._peek() == "]":
            self._index += 1
            self._depth -= 1
            return JsonArray(location=self._location_at(start), elements=elements)

        while True:
            self._skip_whitespace()
            elements.append(self._parse_value())
            self._skip_whitespace()
            character = self._peek()
            if character == ",":
                self._index += 1
                self._skip_whitespace()
                if self._peek() == "]":
                    raise self._error("trailing comma before ']'")
                continue
            if character == "]":
                self._index += 1
                break
            found = self._peek() or "end of file"
            raise self._error(f"expected ',' or ']' but found {found!r}")

        self._depth -= 1
        return JsonArray(location=self._location_at(start), elements=elements)

    def _parse_object(self) -> JsonObject:
        start = self._index
        self._expect("{")
        self._depth += 1
        members: dict[str, JsonNode] = {}
        key_locations: dict[str, Location] = {}

        self._skip_whitespace()
        if self._peek() == "}":
            self._index += 1
            self._depth -= 1
            return JsonObject(
                location=self._location_at(start), members=members, key_locations=key_locations
            )

        while True:
            self._skip_whitespace()
            key_location = self._here()
            if self._peek() != '"':
                found = self._peek() or "end of file"
                raise self._error(f"expected a quoted key but found {found!r}")
            key = self._parse_string()
            if key in members:
                previous = key_locations[key]
                raise JsonParseError(
                    f"duplicate key {key!r}; first defined at {previous}", key_location
                )
            self._skip_whitespace()
            self._expect(":")
            self._skip_whitespace()
            members[key] = self._parse_value()
            key_locations[key] = key_location

            self._skip_whitespace()
            character = self._peek()
            if character == ",":
                self._index += 1
                self._skip_whitespace()
                if self._peek() == "}":
                    raise self._error("trailing comma before '}'")
                continue
            if character == "}":
                self._index += 1
                break
            found = self._peek() or "end of file"
            raise self._error(f"expected ',' or '}}' but found {found!r}")

        self._depth -= 1
        return JsonObject(
            location=self._location_at(start), members=members, key_locations=key_locations
        )

    def _parse_string(self) -> str:
        self._expect('"')
        pieces: list[str] = []
        while True:
            if self._at_end():
                raise self._error("unterminated string")
            character = self._text[self._index]
            if character == '"':
                self._index += 1
                return "".join(pieces)
            if character == "\\":
                self._index += 1
                pieces.append(self._parse_escape())
                continue
            if character < " ":
                raise self._error(f"unescaped control character U+{ord(character):04X} in string")
            self._index += 1
            pieces.append(character)

    def _parse_escape(self) -> str:
        if self._at_end():
            raise self._error("unterminated escape sequence")
        character = self._text[self._index]
        simple = _SIMPLE_ESCAPES.get(character)
        if simple is not None:
            self._index += 1
            return simple
        if character != "u":
            raise self._error(f"unknown escape sequence '\\{character}'")

        self._index += 1
        code = self._parse_hex4()
        if _HIGH_SURROGATE_FIRST <= code <= _HIGH_SURROGATE_LAST:
            low = self._try_parse_low_surrogate()
            if low is not None:
                combined = 0x10000 + ((code - _HIGH_SURROGATE_FIRST) << 10)
                combined += low - _LOW_SURROGATE_FIRST
                return chr(combined)
        return chr(code)

    def _try_parse_low_surrogate(self) -> int | None:
        if not self._text.startswith("\\u", self._index):
            return None
        saved = self._index
        self._index += 2
        code = self._parse_hex4()
        if _LOW_SURROGATE_FIRST <= code <= _LOW_SURROGATE_LAST:
            return code
        self._index = saved
        return None

    def _parse_hex4(self) -> int:
        digits = self._text[self._index : self._index + 4]
        if len(digits) < 4:
            raise self._error("truncated \\u escape")
        for digit in digits:
            if digit not in "0123456789abcdefABCDEF":
                raise self._error(f"invalid hex digit {digit!r} in \\u escape")
        self._index += 4
        return int(digits, 16)
