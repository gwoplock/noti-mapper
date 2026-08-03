import json
from pathlib import Path

import pytest

from noti_mapper.jsonloc import (
    parse,
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
