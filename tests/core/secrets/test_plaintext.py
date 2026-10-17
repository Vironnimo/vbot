"""Conversion of the plaintext credential files (.env, oauth/*.json) into the Secret Store."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Literal

import pytest

from core.database import capture_config_backup, list_config_backups, write_bootstrap_marker
from core.database import config_backups as config_backups_module
from core.providers.token_store import TokenStore
from core.secrets import (
    CREDENTIALS,
    OAUTH_TOKENS,
    PlaintextImport,
    SecretStoreUnavailableError,
    credential_file_reports,
    ignored_plaintext_files,
    import_plaintext_credentials,
    retire_plaintext_credentials,
    secret_store_health,
    settle_plaintext_credentials,
)
from tests.core.secrets.secrets_test_support import TOKEN_TEXT, data_directory, open_store
from tests.directory_links import link_directory

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
        "SPLIT_VALUE=one two\n"
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
        "SPLIT_VALUE": "one two",
    }
    login = TokenStore(store).load("github-copilot", "oauth")
    assert login is not None and login.access_token == "token-value"
    assert store.status().import_pending
    # A rolled-back update leaves the previous version its files.
    assert _contents(env, token) == files

    written = store.path.read_bytes()
    assert import_plaintext_credentials(store) == imported
    assert store.path.read_bytes() == written

    # While the import is pending, the store follows the files, but only where it
    # still holds what the import put there: a value set since stays.
    store.set_entry(CREDENTIALS, "ANTHROPIC_API_KEY", "sk-ant-set-in-the-webui")
    env.write_text("OPENAI_API_KEY=sk-rotated\nANTHROPIC_API_KEY=sk-ant-stale\n", encoding="utf-8")
    token.unlink()
    mirrored = import_plaintext_credentials(open_store(tmp_path))

    assert mirrored is not None
    assert mirrored.removed == ("SPLIT_VALUE", "oauth/github-copilot-oauth")
    assert mirrored.conflicts == ("ANTHROPIC_API_KEY",)
    assert store.entries(CREDENTIALS) == {
        "ANTHROPIC_API_KEY": "sk-ant-set-in-the-webui",
        "OPENAI_API_KEY": "sk-rotated",
    }
    assert store.entries(OAUTH_TOKENS) == {}

    # `convert` imports without removing anything, and never over a newer value.
    env.write_text("ANTHROPIC_API_KEY=sk-ant-stale\n", encoding="utf-8")
    converted = import_plaintext_credentials(open_store(tmp_path), remove_missing=False)

    assert converted is not None
    assert (converted.removed, converted.conflicts) == ((), ("ANTHROPIC_API_KEY",))
    assert store.entries(CREDENTIALS) == {
        "ANTHROPIC_API_KEY": "sk-ant-set-in-the-webui",
        "OPENAI_API_KEY": "sk-rotated",
    }


@pytest.mark.parametrize(
    ("relative", "content", "skipped", "recorded"),
    [
        (
            ".env",
            "OPENAI_API_KEY=sk-openai\nexport SHELL_STYLE=value\nc2VjcmV0/dG9rZW4=\n",
            {".env:2": "not a credential name", ".env:3": "not a credential name"},
            True,
        ),
        (".env", b"OPENAI_API_KEY=sk-\xff\n", {".env": "not valid UTF-8"}, False),
        (TOKEN, '{"format_version": 1', {TOKEN: "not valid JSON"}, False),
        ("oauth/legacy.json", '{"access_token": "x"}', None, False),
    ],
    ids=["invalid names", "not UTF-8", "token not JSON", "token before Generation 1"],
)
def test_a_file_with_entries_that_cannot_be_imported_stays_and_is_reported(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    relative: str,
    content: str | bytes,
    skipped: dict[str, str] | None,
    recorded: bool,
) -> None:
    data_dir = _data_dir(tmp_path)
    path = _write(data_dir, relative, content)
    store = open_store(tmp_path)

    imported = import_plaintext_credentials(store)
    retired = retire_plaintext_credentials(store)

    assert imported is not None
    assert imported.sources == ((relative,) if recorded else ())
    if skipped is not None:
        assert dict(imported.skipped) == skipped
    else:
        assert imported.skipped[relative].startswith("does not load as an OAuth token")
    if recorded:
        assert retired is not None and retired.deleted == ()
    else:
        assert retired is None
    assert _contents(path) == {path: content if isinstance(content, bytes) else content.encode()}
    assert ignored_plaintext_files(store) == (relative,)
    assert store.import_record() is None
    # The status names the lines and how to resolve them, never their text: a line
    # that is no assignment may be a pasted secret.
    reason = str(secret_store_health(store)["degraded_reason"])
    assert relative in reason and "fix or remove what cannot be imported" in reason
    if recorded:
        assert "line 2: not a credential name" in reason
        assert "line 3: not a credential name" in reason
    for text in ("SHELL_STYLE", "c2VjcmV0", "sk-"):
        assert text not in reason and text not in caplog.text


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
    # Token files an older vBot's snapshot restore moved to quarantine: a copy of
    # what the store holds goes, another login stays and is reported.
    batch = data_dir / "quarantine" / "json-documents" / "20250101T000000-0a1b2c3d"
    _write(batch, TOKEN, TOKEN_TEXT)
    _write(batch, "oauth/older-login.json", TOKEN_TEXT.replace("token-value", "older"))
    store = open_store(tmp_path)
    import_plaintext_credentials(store)
    changed.write_text(TOKEN_TEXT.replace("token-value", "edited"), encoding="utf-8")

    retired = retire_plaintext_credentials(store)

    assert retired is not None
    assert retired.deleted == (".env", TOKEN)
    assert dict(retired.kept) == {"oauth/openai-codex.json": "changed after the import"}
    assert (retired.backup_entries, retired.backups_complete) == (3, True)
    assert retired.quarantine_copies == 1
    assert not env.exists() and not token.exists() and changed.exists()
    assert all(not backup.files for backup in list_config_backups(data_dir))
    assert store.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
    assert sorted(store.entries(OAUTH_TOKENS)) == ["github-copilot-oauth", "openai-codex"]
    assert store.import_record() is None
    assert ignored_plaintext_files(store) == ("oauth/openai-codex.json",)
    assert retire_plaintext_credentials(store) is None
    health = secret_store_health(store)
    assert health["plaintext_copies"] == [
        "quarantine/json-documents/20250101T000000-0a1b2c3d/oauth/older-login.json"
    ]
    assert "document quarantine" in str(health["degraded_reason"])
    assert "which keeps its values: oauth/openai-codex" in str(health["degraded_reason"])
    warned = {report.file_path for report in credential_file_reports(data_dir)}
    assert warned == {changed, batch / "oauth" / "older-login.json"}


@pytest.mark.parametrize("interruption", ["a file cannot be deleted", "the process dies"])
def test_a_partial_or_interrupted_retire_never_lets_a_later_import_remove_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    data_dir = _data_dir(tmp_path)
    env = _write(data_dir, ".env", "OPENAI_API_KEY=sk-openai\nBRAVE_API_KEY=brave\n")
    token = _write(data_dir, TOKEN, TOKEN_TEXT)
    store = open_store(tmp_path)
    settle_plaintext_credentials(store, mode="verification")

    class _Died(BaseException):
        pass

    unlink = Path.unlink

    def failing_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == token:
            if interruption == "the process dies":
                raise _Died
            raise PermissionError(13, "Access is denied", str(path))
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    if interruption == "the process dies":
        with pytest.raises(_Died):
            retire_plaintext_credentials(store)
    else:
        retired = retire_plaintext_credentials(store)
        assert retired is not None and retired.deleted == (".env",)
        assert retired.kept[TOKEN].startswith("cannot be deleted")
    monkeypatch.undo()

    # The record never names a file once it is deleted.
    assert not env.exists() and token.exists()
    record = store.import_record()
    assert record is not None and ".env" not in record["sources"]
    # The next update's verification start imports what is left, removing nothing.
    settle_plaintext_credentials(open_store(tmp_path), mode="verification")
    mirrored = import_plaintext_credentials(open_store(tmp_path))

    assert mirrored is not None and mirrored.removed == ()
    assert store.entries(CREDENTIALS) == {"BRAVE_API_KEY": "brave", "OPENAI_API_KEY": "sk-openai"}
    assert sorted(store.entries(OAUTH_TOKENS)) == ["github-copilot-oauth"]


def test_a_linked_plaintext_file_is_imported_but_never_deleted(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    shared = tmp_path / "shared"
    shared_token = _write(shared, "github-copilot-oauth.json", TOKEN_TEXT)
    link_directory(data_dir / "oauth", shared)
    shared_env = _write(tmp_path, "shared.env", "OPENAI_API_KEY=sk-openai\n")
    env_linked = True
    try:
        (data_dir / ".env").symlink_to(shared_env)
    except OSError:
        env_linked = False
    store = open_store(tmp_path)

    settle_plaintext_credentials(store, mode="verification")
    settle_plaintext_credentials(open_store(tmp_path), mode="normal")

    assert sorted(store.entries(OAUTH_TOKENS)) == ["github-copilot-oauth"]
    assert shared_token.read_text(encoding="utf-8") == TOKEN_TEXT
    assert (data_dir / TOKEN).exists()
    expected = [TOKEN]
    if env_linked:
        assert store.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
        assert (data_dir / ".env").is_symlink() and shared_env.exists()
        expected.insert(0, ".env")
    health = secret_store_health(store)
    assert health["plaintext_files"] == expected
    reason = str(health["degraded_reason"])
    assert "lies in the oauth directory, which is a link" in reason
    assert "its credentials are in the Secret Store" in reason
    reports = credential_file_reports(data_dir)
    assert len(reports) == len(expected)
    assert all("vBot never deletes it" in report.diagnostics[0].message for report in reports)


@pytest.mark.skipif(sys.platform != "win32", reason="names ignore case on Windows only")
def test_on_windows_an_imported_name_takes_the_spelling_of_the_environment(
    tmp_path: Path,
) -> None:
    data_dir = _data_dir(tmp_path)
    _write(data_dir, ".env", "openai_api_key=sk-lower\nOpenAI_Api_Key=sk-later\n")
    store = open_store(tmp_path)

    imported = import_plaintext_credentials(store)

    assert imported is not None and imported.credentials == ("OPENAI_API_KEY",)
    assert store.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-later"}


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
    assert all("files=.env" in message for message in warnings)
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


def test_a_store_in_quarantine_is_reported_while_secrets_json_is_missing(tmp_path: Path) -> None:
    data_dir = _data_dir(tmp_path)
    store = open_store(tmp_path)
    store.set_entry(CREDENTIALS, "OPENAI_API_KEY", "sk-openai")
    # Where the document-set restore of an older vBot moved it.
    batch = data_dir / "quarantine" / "json-documents" / "20260101T000000-0a1b2c3d"
    batch.mkdir(parents=True)
    store.path.replace(batch / "secrets.json")

    health = secret_store_health(open_store(tmp_path))
    assert health["state"] == "absent"
    assert health["quarantined_stores"] == [
        "quarantine/json-documents/20260101T000000-0a1b2c3d/secrets.json"
    ]
    assert "move it back to <data-dir>/secrets.json" in str(health["degraded_reason"])
    [report] = credential_file_reports(data_dir)
    assert report.file_path == store.path
    assert "a Secret Store lies in quarantine" in report.diagnostics[0].message

    (batch / "secrets.json").replace(store.path)
    restored = open_store(tmp_path)
    assert restored.entries(CREDENTIALS) == {"OPENAI_API_KEY": "sk-openai"}
    assert secret_store_health(restored)["degraded_reason"] is None
    assert credential_file_reports(data_dir) == []
