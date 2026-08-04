import json
from pathlib import Path

import pytest

from noti_mapper.secrets import (
    SecretsError,
    SecretStore,
    empty_store,
    load_secrets,
    references_in,
    substitute,
)


def _write_secrets(tmp_path: Path, values: object, *, mode: int = 0o600) -> Path:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    path.chmod(mode)
    return path


def test_a_missing_secrets_file_is_not_an_error(tmp_path: Path) -> None:
    store = load_secrets(tmp_path / "absent.json")
    assert store.values == {}
    assert store.get("anything") is None


def test_secrets_load_from_a_private_file(tmp_path: Path) -> None:
    path = _write_secrets(tmp_path, {"porch_mail_password": "hunter2"})
    store = load_secrets(path)
    assert store.get("porch_mail_password") == "hunter2"
    assert store.names() == ["porch_mail_password"]


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o660])
def test_group_or_world_readable_secrets_are_refused(tmp_path: Path, mode: int) -> None:
    path = _write_secrets(tmp_path, {"a": "b"}, mode=mode)
    with pytest.raises(SecretsError) as caught:
        load_secrets(path)
    message = str(caught.value)
    assert f"{mode:04o}" in message
    assert "chmod 0600" in message


def test_mode_0400_is_accepted(tmp_path: Path) -> None:
    path = _write_secrets(tmp_path, {"a": "b"}, mode=0o400)
    assert load_secrets(path).get("a") == "b"


def test_non_object_secrets_file_is_refused(tmp_path: Path) -> None:
    path = _write_secrets(tmp_path, ["a", "b"])
    with pytest.raises(SecretsError, match="top level must be an object"):
        load_secrets(path)


def test_non_string_secret_value_is_refused(tmp_path: Path) -> None:
    path = _write_secrets(tmp_path, {"a": 1})
    with pytest.raises(SecretsError, match="must be a string"):
        load_secrets(path)


def test_malformed_secrets_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(SecretsError, match="line 1"):
        load_secrets(path)


def test_secret_names_are_restricted(tmp_path: Path) -> None:
    path = _write_secrets(tmp_path, {"has space": "x"})
    with pytest.raises(SecretsError, match="outside"):
        load_secrets(path)


def test_substitution_replaces_references() -> None:
    store = SecretStore(path=Path("s.json"), values={"pw": "hunter2", "host": "mail.example.net"})
    result = substitute(text="${secret:pw}", store=store)
    assert result.text == "hunter2"
    assert result.missing == ()


def test_substitution_works_inside_a_larger_string() -> None:
    store = SecretStore(path=Path("s.json"), values={"user": "alice", "pw": "hunter2"})
    result = substitute(text="imaps://${secret:user}:${secret:pw}@host", store=store)
    assert result.text == "imaps://alice:hunter2@host"


def test_missing_secrets_are_reported_and_left_in_place() -> None:
    store = empty_store(Path("s.json"))
    result = substitute(text="${secret:pw} and ${secret:pw}", store=store)
    assert result.missing == ("pw",)
    assert result.text == "${secret:pw} and ${secret:pw}"


def test_strings_without_references_are_untouched() -> None:
    store = empty_store(Path("s.json"))
    result = substitute(text="plain text $ {secret:pw} ${notasecret}", store=store)
    assert result.text == "plain text $ {secret:pw} ${notasecret}"
    assert result.missing == ()


def test_references_in_lists_names_once_in_order() -> None:
    assert references_in("${secret:b} ${secret:a} ${secret:b}") == ["b", "a"]
    assert references_in("nothing here") == []
