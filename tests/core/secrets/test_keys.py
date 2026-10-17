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
from core.secrets import _keys as keys_module
from tests.core.secrets.secrets_test_support import data_directory


def _store(data_dir: Path, environ: dict[str, str]) -> SecretStore:
    return SecretStore(data_dir, protections=(KeyFileProtection(environ=environ),))


def test_a_new_key_file_lives_where_every_lookup_searches_and_is_never_overwritten(
    tmp_path: Path,
) -> None:
    data_dir = data_directory(tmp_path)
    # An update's verification start inherits the shell that ran `vbot update`.
    configured = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    _store(data_dir, configured).set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    status = _store(data_dir, configured).status()
    default = Path.home() / ".config" / "vbot" / "keys" / f"{status.store_id}.key"
    legacy = tmp_path / "xdg" / "vbot" / "keys" / default.name
    assert status.key_location == str(default)
    assert status.key_locations == (str(default), str(legacy))
    assert status.new_key_location == str(default.parent)

    # The service started later, without XDG_CONFIG_HOME, finds the key all the same.
    for environ in ({}, configured):
        assert _store(data_dir, environ).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"

    content = default.read_bytes()
    with pytest.raises(KeyUnavailableError, match="never overwritten"):
        KeyFileProtection(environ={}).create(str(status.store_id))
    assert default.read_bytes() == content

    # A key an earlier build created under XDG_CONFIG_HOME is still found there.
    legacy.parent.mkdir(parents=True)
    default.replace(legacy)
    assert _store(data_dir, configured).entry(CREDENTIALS, "OPENAI_API_KEY") == "sk-openai"


def test_a_provisioned_key_file_is_adopted_or_created_where_it_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provisioned = tmp_path / "credentials" / "vbot.key"
    environ = {KEY_FILE_ENVIRONMENT_VARIABLE: str(provisioned)}
    first = _store(data_directory(tmp_path, "first"), environ)
    first.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-first")
    content = provisioned.read_bytes()

    # The second store looks while the file is still missing, as a store created
    # at the same moment in another process does: it adopts the first one's key.
    with monkeypatch.context() as racing:
        racing.setattr(keys_module, "_exists", lambda _path: False)
        second = _store(data_directory(tmp_path, "second"), environ)
        second.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-second")

    assert provisioned.read_bytes() == content
    assert [path.name for path in provisioned.parent.iterdir()] == ["vbot.key"]
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
