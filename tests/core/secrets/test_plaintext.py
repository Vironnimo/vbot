"""Conversion of the plaintext credential files (.env, oauth/*.json) into the Secret Store."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import pytest

from core.database import capture_config_backup, list_config_backups, write_bootstrap_marker
from core.database import config_backups as config_backups_module
from core.providers.token_store import TokenStore
from core.secrets import (
    CONVERT_COMMAND,
    CREDENTIALS,
    OAUTH_TOKENS,
    PlaintextImport,
    SecretStoreUnavailableError,
    ignored_plaintext_files,
    import_plaintext_credentials,
    retire_plaintext_credentials,
    secret_store_health,
    settle_plaintext_credentials,
)
from tests.core.secrets.secrets_test_support import TOKEN_TEXT, data_directory, open_store

TOKEN = "oauth/github-copilot-oauth.json"


def _data_dir(tmp_path: Path) -> Path:
    root = data_directory(tmp_path)
    write_bootstrap_marker(root)
    return root


def _write(data_dir: Path, relative: str, content: str | bytes) -> Path:
    path = data_dir.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    return path


def _contents(*paths: Path) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def test_an_import_mirrors_the_plaintext_files_and_changes_none_of_them(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    env = _write(
        data_dir,
        ".env",
        "# provider keys\r\n"
        "OPENAI_API_KEY=sk-openai\r\n"
        'ANTHROPIC_API_KEY="sk-ant=with=equals"\r\n'
        "TEMPLATE_PLACEHOLDER=\r\n"
        "SPLIT_VALUE=one two\n"
        "OPENAI_API_KEY=sk-openai-later\n",
    )
    token = _write(data_dir, TOKEN, TOKEN_TEXT)
    files = _contents(env, token)
    store = open_store(tmp_path)

    imported = import_plaintext_credentials(store)

    assert imported == PlaintextImport(
        sources=(".env", TOKEN),
        credentials=("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SPLIT_VALUE"),
        oauth_tokens=("github-copilot-oauth",),
    )
    assert store.entries(CREDENTIALS) == {
        "ANTHROPIC_API_KEY": "sk-ant=with=equals",
        "OPENAI_API_KEY": "sk-openai-later",
        "SPLIT_VALUE": "one two",
    }
    login = TokenStore(store).load("github-copilot", "oauth")
    assert login is not None and login.access_token == "token-value"
    assert store.status().import_pending
    # A rolled-back update leaves the previous version its files.
    assert _contents(env, token) == files

    written = store.path.read_bytes()
    assert import_plaintext_credentials(store) == imported
    assert store.path.read_bytes() == written

    # While the import is pending, the store follows the files.
    env.write_text("OPENAI_API_KEY=sk-rotated\n", encoding="utf-8")
    token.unlink()
    mirrored = import_plaintext_credentials(open_store(tmp_path))

    assert mirrored is not None
    assert mirrored.removed == ("ANTHROPIC_API_KEY", "SPLIT_VALUE", "oauth/github-copilot-oauth")
    assert store.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-rotated"}
    assert store.entries(OAUTH_TOKENS) == {}


@pytest.mark.parametrize(
    ("relative", "content", "skipped"),
    [
        (
            ".env",
            "OPENAI_API_KEY=sk-openai\nexport SHELL_STYLE=value\n",
            {".env:export SHELL_STYLE": "not a credential name"},
        ),
        (".env", b"OPENAI_API_KEY=sk-\xff\n", {".env": "not valid UTF-8"}),
        (TOKEN, '{"format_version": 1', {TOKEN: "not valid JSON"}),
        ("oauth/legacy.json", '{"access_token": "x"}', None),
    ],
    ids=["invalid name", "not UTF-8", "token not JSON", "token before Generation 1"],
)
def test_a_file_with_entries_that_cannot_be_imported_stays_and_is_reported(
    tmp_path: Path, relative: str, content: str | bytes, skipped: dict[str, str] | None
) -> None:
    data_dir = _data_dir(tmp_path)
    path = _write(data_dir, relative, content)
    store = open_store(tmp_path)

    imported = import_plaintext_credentials(store)
    retired = retire_plaintext_credentials(store)

    assert imported is not None and imported.sources == ()
    if skipped is not None:
        assert dict(imported.skipped) == skipped
    else:
        assert imported.skipped[relative].startswith("does not load as an OAuth token")
    assert retired is not None and retired.deleted == ()
    assert _contents(path) == {path: content if isinstance(content, bytes) else content.encode()}
    assert ignored_plaintext_files(store) == (relative,)
    assert store.import_record() is None


def test_retiring_deletes_only_unchanged_imported_files_and_their_backup_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = _data_dir(tmp_path)
    env = _write(data_dir, ".env", "OPENAI_API_KEY=sk-openai\n")
    token = _write(data_dir, TOKEN, TOKEN_TEXT)
    changed = _write(data_dir, "oauth/openai-codex.json", TOKEN_TEXT)
    # A backup an older vBot took, when the plaintext files were configuration files.
    with monkeypatch.context() as older:
        older.setattr(
            config_backups_module,
            "CONFIGURATION_FILE_PATTERNS",
            (*config_backups_module.CONFIGURATION_FILE_PATTERNS, ".env", "oauth/*.json"),
        )
        assert capture_config_backup(data_dir, reason="older") is not None
    store = open_store(tmp_path)
    import_plaintext_credentials(store)
    changed.write_text(TOKEN_TEXT.replace("token-value", "edited"), encoding="utf-8")

    retired = retire_plaintext_credentials(store)

    assert retired is not None
    assert retired.deleted == (".env", TOKEN)
    assert dict(retired.kept) == {"oauth/openai-codex.json": "changed after the import"}
    assert (retired.backup_entries, retired.backups_complete) == (3, True)
    assert not env.exists() and not token.exists() and changed.exists()
    assert all(not backup.files for backup in list_config_backups(data_dir))
    assert store.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
    assert sorted(store.entries(OAUTH_TOKENS)) == ["github-copilot-oauth", "openai-codex"]
    assert store.import_record() is None
    assert ignored_plaintext_files(store) == ("oauth/openai-codex.json",)
    assert retire_plaintext_credentials(store) is None


def test_an_update_imports_while_it_verifies_and_deletes_once_it_is_committed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = _data_dir(tmp_path)
    settle_plaintext_credentials(open_store(tmp_path), mode="verification")
    assert not (data_dir / "secrets.json").exists()
    env = _write(data_dir, ".env", "OPENAI_API_KEY=sk-openai\n")
    token = _write(data_dir, TOKEN, TOKEN_TEXT)
    files = _contents(env, token)

    # The new version's verification start, then a rollback and a second attempt.
    for _attempt in range(2):
        settle_plaintext_credentials(open_store(tmp_path), mode="verification")
        assert _contents(env, token) == files
    # The first normal start once the new version is active.
    settle_plaintext_credentials(open_store(tmp_path), mode="normal")

    assert _contents(env, token) == {env: None, token: None}
    assert not (data_dir / "oauth").exists()
    store = open_store(tmp_path)
    assert store.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
    assert store.import_record() is None

    # A plaintext file that appears later is ignored and never read, in every mode.
    later = _write(data_dir, ".env", "OPENAI_API_KEY=sk-later\n")
    caplog.clear()
    modes: tuple[Literal["verification", "normal", "test"], ...] = (
        "verification",
        "normal",
        "test",
    )
    for mode in modes:
        settle_plaintext_credentials(open_store(tmp_path), mode=mode)

    assert later.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-later\n"
    assert open_store(tmp_path).entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 3
    assert all(CONVERT_COMMAND in message and "files=.env" in message for message in warnings)
    assert "sk-later" not in caplog.text


def test_an_unopenable_store_keeps_every_plaintext_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir = _data_dir(tmp_path)
    env = _write(data_dir, ".env", "OPENAI_API_KEY=sk-openai\n")
    store = open_store(tmp_path)
    settle_plaintext_credentials(store, mode="verification")
    Path(str(store.status().key_location)).unlink()
    files = _contents(env, store.path)

    unavailable = open_store(tmp_path)
    with pytest.raises(SecretStoreUnavailableError):
        import_plaintext_credentials(unavailable)
    with pytest.raises(SecretStoreUnavailableError):
        settle_plaintext_credentials(unavailable, mode="verification")
    settle_plaintext_credentials(unavailable, mode="normal")

    assert _contents(env, store.path) == files
    assert "were not removed; the next start tries again" in caplog.text
    health = secret_store_health(unavailable)
    assert health["state"] == "unavailable"
    assert health["plaintext_files"] == []
    assert "the Secret Store is unavailable" in str(health["degraded_reason"])
