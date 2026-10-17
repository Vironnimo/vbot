"""Protection of the Secret Store's data key: key files and Windows DPAPI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.secrets import (
    CREDENTIALS,
    KEY_FILE_ENVIRONMENT_VARIABLE,
    DpapiKeyProtection,
    KeyFileProtection,
    KeyUnavailableError,
    SecretStore,
    SecretStoreError,
)
from tests.core.secrets.secrets_test_support import data_directory


def _store(data_dir: Path, environ: dict[str, str]) -> SecretStore:
    return SecretStore(data_dir, protections=(KeyFileProtection(environ=environ),))


def test_a_key_file_lives_in_the_configuration_home_and_is_never_overwritten(
    tmp_path: Path,
) -> None:
    data_dir = data_directory(tmp_path)
    configured = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    _store(data_dir, configured).set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    store_id = _store(data_dir, configured).status().store_id
    key_file = tmp_path / "xdg" / "vbot" / "keys" / f"{store_id}.key"
    assert _store(data_dir, configured).status().key_location == str(key_file)

    # A service without XDG_CONFIG_HOME and a shell with it both find the default location.
    default = Path.home() / ".config" / "vbot" / "keys" / key_file.name
    default.parent.mkdir(parents=True)
    key_file.replace(default)
    for environ in ({}, configured):
        assert _store(data_dir, environ).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"

    content = default.read_bytes()
    with pytest.raises(KeyUnavailableError, match="never overwritten"):
        KeyFileProtection(environ={}).create(str(store_id))
    assert default.read_bytes() == content


def test_a_provisioned_key_file_is_adopted_or_created_where_it_is_named(tmp_path: Path) -> None:
    provisioned = tmp_path / "credentials" / "vbot.key"
    environ = {KEY_FILE_ENVIRONMENT_VARIABLE: str(provisioned)}
    first = _store(data_directory(tmp_path, "first"), environ)
    first.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-first")
    content = provisioned.read_bytes()

    second = _store(data_directory(tmp_path, "second"), environ)
    second.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-second")

    assert provisioned.read_bytes() == content
    assert first.status().key_location == second.status().key_location == str(provisioned)
    assert _store(first.data_dir, environ).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-first"
    assert _store(second.data_dir, environ).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-second"

    # A provisioned file that holds no vBot key is reported, never replaced.
    provisioned.write_text("not a key\n", encoding="utf-8")
    third = _store(data_directory(tmp_path, "third"), environ)
    with pytest.raises(SecretStoreError, match="not valid JSON"):
        third.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-third")
    assert provisioned.read_text(encoding="utf-8") == "not a key\n"
    assert not third.path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI")
def test_dpapi_wraps_the_key_inside_the_store_for_this_account(tmp_path: Path) -> None:
    data_dir = data_directory(tmp_path)
    SecretStore(data_dir).set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")

    document = json.loads((data_dir / "secrets.json").read_text(encoding="utf-8"))
    assert document["key"]["protection"] == "dpapi"
    assert document["key"]["wrapped"]
    assert SecretStore(data_dir).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"
    assert not (Path.home() / ".config").exists()
    # The wrapped key is bound to its store: under another store id it does not open.
    with pytest.raises(KeyUnavailableError, match="could not unwrap"):
        DpapiKeyProtection().load("another-store", document["key"]["id"], document["key"])
