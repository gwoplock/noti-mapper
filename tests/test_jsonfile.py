import json
from pathlib import Path

import pytest

from noti_mapper.jsonfile import JsonFileError, parse, read_object


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_ordinary_json_parses_the_way_the_standard_library_does() -> None:
    text = '{"a": [1, 2.5, true, null], "b": {"c": "x"}}'
    assert parse(text=text, path=Path("a.json")) == json.loads(text)


def test_read_object_returns_the_document(tmp_path: Path) -> None:
    path = _write(tmp_path, '{"instances": {}}')
    assert read_object(path) == {"instances": {}}


def test_duplicate_keys_in_one_object_are_an_error() -> None:
    with pytest.raises(JsonFileError) as caught:
        parse(text='{"a": 1, "a": 2}', path=Path("a.json"))
    assert caught.value.detail == "duplicate key 'a'"


def test_duplicate_keys_are_caught_at_any_depth() -> None:
    with pytest.raises(JsonFileError, match="duplicate key 'plugin'"):
        parse(text='{"x": {"plugin": 1, "plugin": 2}}', path=Path("a.json"))


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_the_things_python_accepts_but_json_does_not_are_rejected(literal: str) -> None:
    with pytest.raises(JsonFileError, match="not valid JSON"):
        parse(text=f'{{"n": {literal}}}', path=Path("a.json"))


@pytest.mark.parametrize("text", ['{"a": 1,}', "// comment\n{}", "{'a': 1}", '{"a" 1}', "{"])
def test_malformed_documents_are_rejected(text: str) -> None:
    with pytest.raises(JsonFileError):
        parse(text=text, path=Path("a.json"))


def test_a_syntax_error_keeps_the_position_the_standard_library_gives() -> None:
    with pytest.raises(JsonFileError) as caught:
        parse(text='{\n  "instances": {,\n}\n', path=Path("a.json"))
    assert "line 2" in caught.value.detail
    assert "column 17" in caught.value.detail


def test_the_file_is_named_separately_from_the_problem(tmp_path: Path) -> None:
    path = _write(tmp_path, "{")
    with pytest.raises(JsonFileError) as caught:
        read_object(path)

    assert caught.value.path == path
    assert str(path) not in caught.value.detail
    assert str(path) in str(caught.value)


def test_a_top_level_array_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, "[]")
    with pytest.raises(JsonFileError, match="top level must be an object"):
        read_object(path)


def test_an_unreadable_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(JsonFileError, match="cannot be read"):
        read_object(tmp_path / "absent.json")
