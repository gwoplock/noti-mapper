import json
from pathlib import Path

import pytest

from noti_mapper.jsonloc import (
    JsonArray,
    JsonObject,
    JsonScalar,
    parse,
    parse_file,
    to_plain,
)

_DOCUMENTS = [
    "{}",
    "[]",
    '{"a": 1}',
    '{"a": [1, 2, 3], "b": {"c": null}}',
    '{"a": true, "b": false, "c": null}',
    '{"n": [0, -1, 1.5, -2.25e10, 1E+3, 0.0]}',
    '{"s": "plain", "e": "quote:\\" backslash:\\\\ slash:\\/ \\b\\f\\n\\r\\t"}',
    '{"u": "\\u0041\\u00e9\\u4e2d"}',
    '{"emoji": "\\ud83d\\ude00"}',
    '  {\n  "spaced" :\t[ 1 ,2 ]\n}  ',
    '"top level string"',
    "42",
    "true",
    "null",
]


@pytest.mark.parametrize("text", _DOCUMENTS)
def test_parsed_values_agree_with_the_standard_library(text: str) -> None:
    node = parse(text=text, path=Path("t.json"))
    assert to_plain(node) == json.loads(text)


def test_locations_point_at_the_right_lines() -> None:
    text = '{\n  "instances": {\n    "Porch Mail": {\n      "plugin": "imap-input"\n    }\n  }\n}\n'
    document = parse(text=text, path=Path("a.json"))
    assert isinstance(document, JsonObject)

    assert document.key_locations["instances"].line == 2
    assert document.key_locations["instances"].column == 3

    instances = document.members["instances"]
    assert isinstance(instances, JsonObject)
    assert instances.key_locations["Porch Mail"].line == 3

    porch = instances.members["Porch Mail"]
    assert isinstance(porch, JsonObject)
    plugin = porch.members["plugin"]
    assert isinstance(plugin, JsonScalar)
    assert plugin.location.line == 4
    assert plugin.value == "imap-input"


def test_location_str_is_file_line_column() -> None:
    document = parse(text='{"a": 1}', path=Path("/etc/noti-mapper.d/10-a.json"))
    assert isinstance(document, JsonObject)
    assert str(document.key_locations["a"]) == "/etc/noti-mapper.d/10-a.json:1:2"


def test_arrays_and_objects_carry_their_opening_location() -> None:
    document = parse(text='{\n  "a": [\n    1\n  ]\n}', path=Path("a.json"))
    assert isinstance(document, JsonObject)
    array = document.members["a"]
    assert isinstance(array, JsonArray)
    assert array.location.line == 2
    assert array.elements[0].location.line == 3


def test_parse_file_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    node = parse_file(path)
    assert to_plain(node) == {"a": 1}
    assert isinstance(node, JsonObject)
    assert node.location.path == path


def test_integers_stay_integers_and_floats_stay_floats() -> None:
    document = parse(text='{"i": 3, "f": 3.0, "e": 3e0}', path=Path("a.json"))
    assert isinstance(document, JsonObject)
    values = {
        key: node.value for key, node in document.members.items() if isinstance(node, JsonScalar)
    }
    assert isinstance(values["i"], int)
    assert isinstance(values["f"], float)
    assert isinstance(values["e"], float)
