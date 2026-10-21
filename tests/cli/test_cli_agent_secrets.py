"""Tests for ``vbot agent-secrets``: Agent Secrets, Secret requests and Secret Sources over RPC.

The contract that matters most: a value enters only from a hidden prompt or from piped
stdin with ``--value-stdin``, reaches only the RPC params, and leaves only through
``reveal``; what hands out access refuses inside an Agent's Run.
"""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from cli._parser_common import AGENT_SECRET_KINDS, AGENT_SECRET_SINKS
from tests.cli.cli_test_support import FakeRpc, RunCli

_VALUE = "ghp_S3cretValue"


def _secret(name: str = "GITHUB", **changes: Any) -> dict[str, Any]:
    secret: dict[str, Any] = {
        "id": f"sec_{name[:4]}AAAAAAAA",
        "name": name,
        "kind": "login",
        "reference": f"{{{{secret:{name}}}}}",
        "fields": [
            {"name": "username", "concealed": False, "value": "julian"},
            {"name": "password", "concealed": True},
        ],
        "environment": [f"{name}_USERNAME", f"{name}_PASSWORD"],
        "lifetime": "permanent",
        "sinks": ["environment", "terminal", "typing"],
        "agents": ["main"],
        "created_at": "2026-10-20T09:00:00+00:00",
        "created_by": "user",
        "recent_uses": [],
    }
    secret.update(changes)
    return secret


def _token(name: str = "OPENAI_KEY", **changes: Any) -> dict[str, Any]:
    token = {"kind": "token", "fields": [{"name": "value", "concealed": True}]}
    return _secret(name, **{**token, "sinks": ["environment", "files"], **changes})


_REQUEST = {
    "id": "sreq_K7QM4XPA",
    "agent_id": "coder",
    "session_id": "ses_1",
    "project_id": None,
    "name": "NPM_TOKEN",
    "kind": "token",
    "description": "publish the package",
    "lifetime": "session",
    "replaces": False,
    "fields": [{"name": "value", "concealed": True, "optional": False}],
    "created_at": "2026-10-21T10:00:00+00:00",
    "expires_at": "2026-10-21T10:15:00+00:00",
}

_VAULT = {
    "name": "keepassxc",
    "label": "KeePassXC",
    "state": "locked",
    "reason": "",
    "capabilities": ["entries"],
    "unlock_fields": [
        {"name": "password", "label": "Master password", "concealed": True, "required": True},
        {"name": "key_file", "label": "Key file", "concealed": False, "required": False},
    ],
}


def _overview(
    secrets: list[dict[str, Any]] | None = None,
    requests: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {"secrets": secrets or [], "requests": requests or [], "sources": sources or []}


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes = b"", *, terminal: bool = False) -> None:
    monkeypatch.setattr(
        sys, "stdin", SimpleNamespace(isatty=lambda: terminal, buffer=io.BytesIO(data))
    )


def _prompts(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    asked: list[str] = []
    replies = iter(answers)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return next(replies)

    monkeypatch.setattr("getpass.getpass", ask)
    return asked


def test_list_shows_secrets_and_waiting_requests(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "agent_secrets.list",
        _overview(
            [_secret(), _token(agents=[], sinks=[], description="for the build")],
            [_REQUEST],
        ),
    ).reply("agent_secrets.list", _overview())

    code, out, _err = run_cli("agent-secrets", "list")

    lines = out.splitlines()
    assert code == 0
    assert lines[0] == "2 Agent Secrets, by name:"
    assert "GITHUB id=sec_GITHAAAAAAAA" in lines[1]
    assert "agents=main" in lines[1] and "fields=username:julian,password" in lines[1]
    assert "agents=none sinks=none" in lines[2] and "description=for the build" in lines[2]
    assert lines[3] == "1 Secret request waits for the user's answer:"
    assert lines[4].startswith("sreq_K7QM4XPA name=NPM_TOKEN kind=token agent=coder")

    assert run_cli("agent-secrets", "list")[1].splitlines() == [
        "No Agent Secrets.",
        "No Secret request waits for the user.",
    ]


@pytest.mark.parametrize(
    ("argv", "terminal", "stdin", "answers", "values"),
    [
        pytest.param(
            ["OPENAI_KEY"], True, b"", [_VALUE], {"value": _VALUE}, id="token-hidden-prompt"
        ),
        pytest.param(
            ["OPENAI_KEY", "--value-stdin"],
            False,
            b"\xef\xbb\xbf" + _VALUE.encode() + b"\r\n",
            [],
            {"value": _VALUE},
            id="token-stdin",
        ),
        pytest.param(
            ["github", "--user", "julian", "--value-stdin"],
            False,
            f"{_VALUE}\nJBSWY3DPEHPK3PXP\n".encode(),
            [],
            {"password": _VALUE, "totp": "JBSWY3DPEHPK3PXP", "username": "julian"},
            id="login-stdin-with-totp",
        ),
        pytest.param(
            ["GITHUB", "--user", "julian"],
            True,
            b"",
            [_VALUE, ""],
            {"password": _VALUE, "username": "julian"},
            id="login-prompt-without-totp",
        ),
        pytest.param(
            ["SSH_KEY", "--kind", "text", "--value-stdin"],
            False,
            f"-----BEGIN KEY-----\n{_VALUE}\n-----END KEY-----\n".encode(),
            [],
            {"value": f"-----BEGIN KEY-----\n{_VALUE}\n-----END KEY-----\n"},
            id="text-kept-exactly",
        ),
    ],
)
def test_add_takes_the_value_only_from_a_hidden_prompt_or_stdin(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    terminal: bool,
    stdin: bytes,
    answers: list[str],
    values: dict[str, str],
) -> None:
    _stdin(monkeypatch, stdin, terminal=terminal)
    asked = _prompts(monkeypatch, *answers)
    rpc.reply("agent_secrets.list", _overview()).reply(
        "agent_secrets.create", {"secret": _secret(argv[0].upper())}
    )

    code, out, err = run_cli("agent-secrets", "add", *argv)

    params = rpc.params("agent_secrets.create")
    assert code == 0
    assert params["values"] == values
    assert params["name"] == argv[0].upper() and params["agents"] == []
    assert len(asked) == len(answers)
    assert out.startswith(f"Stored the Agent Secret {argv[0].upper()} for main.")
    assert _VALUE not in out + err + "".join(asked)


@pytest.mark.parametrize(
    ("argv", "terminal", "stdin", "fragment"),
    [
        pytest.param(["OPENAI_KEY"], False, b"", "--value-stdin", id="pipe-without-flag"),
        pytest.param(
            ["OPENAI_KEY", "--value-stdin"], False, b"", "stdin has no value", id="empty-stdin"
        ),
        pytest.param(
            ["OPENAI_KEY", "--value-stdin"],
            False,
            f"{_VALUE}\n{_VALUE}\n".encode(),
            "--kind text",
            id="token-with-two-lines",
        ),
        pytest.param(
            ["SSH_KEY", "--kind", "text"], True, b"", "--value-stdin", id="text-in-a-terminal"
        ),
        pytest.param(
            ["OPENAI_KEY", "--value-stdin"],
            False,
            b"\xff" + _VALUE.encode(),
            "UTF-8",
            id="undecodable-stdin",
        ),
    ],
)
def test_add_refuses_unreadable_values_without_storing_or_echoing_them(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    terminal: bool,
    stdin: bytes,
    fragment: str,
) -> None:
    _stdin(monkeypatch, stdin, terminal=terminal)
    rpc.reply("agent_secrets.list", _overview())

    code, out, err = run_cli("agent-secrets", "add", *argv)

    assert code == 1
    assert fragment in out + err and "Nothing was stored." in out + err
    assert _VALUE not in out + err
    assert rpc.methods == ["agent_secrets.list"]


def test_add_refuses_an_existing_name_before_asking_for_the_value(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdin(monkeypatch, terminal=True)
    asked = _prompts(monkeypatch)
    rpc.reply("agent_secrets.list", _overview([_secret()]))

    code, out, err = run_cli("agent-secrets", "add", "github")

    assert code == 1 and asked == []
    assert "already exists (sec_GITHAAAAAAAA)" in out + err
    assert "vbot agent-secrets update GITHUB --value-stdin" in out + err
    assert rpc.methods == ["agent_secrets.list"]


@pytest.mark.parametrize(
    ("agent_flag", "grants"),
    [pytest.param([], ["coder"], id="the-running-agent"), pytest.param(["--agent"], [], id="none")],
)
def test_add_in_a_run_grants_the_running_agent_unless_told_otherwise(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    agent_flag: list[str],
    grants: list[str],
) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "coder")
    _stdin(monkeypatch, _VALUE.encode())
    rpc.reply("agent_secrets.list", _overview()).reply(
        "agent_secrets.create", {"secret": _token(agents=grants)}
    )

    code, _out, _err = run_cli("agent-secrets", "add", "OPENAI_KEY", "--value-stdin", *agent_flag)

    assert code == 0 and rpc.params("agent_secrets.create")["agents"] == grants


def test_update_names_the_secret_by_name_and_reports_what_changed(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("agent_secrets.list", _overview([_secret()]))
    rpc.reply("agent_secrets.update", {"secret": _secret(agents=["main", "coder"])})
    rpc.reply("agent_secrets.update", {"secret": _secret()})

    code, out, _err = run_cli("agent-secrets", "update", "github", "--agent", "main,coder")

    assert code == 0
    assert rpc.params("agent_secrets.update") == {
        "id": "sec_GITHAAAAAAAA",
        "agents": ["main", "coder"],
    }
    assert out.startswith("Updated the Agent Secret GITHUB: granted Agents.")

    unchanged = run_cli("agent-secrets", "update", "GITHUB", "--agent", "main")
    assert unchanged[0] == 0 and "nothing was changed" in unchanged[1]


def test_update_without_a_change_is_refused_before_any_call(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, err = run_cli("agent-secrets", "update", "GITHUB")

    assert code == 1 and "--value-stdin" in out + err and rpc.calls == []


@pytest.mark.parametrize(
    ("change", "allowed"),
    [
        pytest.param(["--agent", "main", "coder"], False, id="more-agents"),
        pytest.param(["--sink", "environment", "files"], False, id="more-sinks"),
        pytest.param(["--sink", "env"], True, id="fewer-sinks"),
        pytest.param(["--agent"], True, id="no-agents"),
    ],
)
def test_update_in_a_run_only_narrows_who_and_where(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    change: list[str],
    allowed: bool,
) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "main")
    rpc.reply("agent_secrets.list", _overview([_secret()])).reply(
        "agent_secrets.update", {"secret": _secret()}
    )

    code, out, err = run_cli("agent-secrets", "update", "GITHUB", *change)

    assert (code == 0) is allowed
    assert ("agent_secrets.update" in rpc.methods) is allowed
    if not allowed:
        assert "Agent's Run" in out + err and "Nothing was changed." in out + err


@pytest.mark.parametrize(
    ("secrets", "target", "fragments"),
    [
        pytest.param([_secret()], "GITHUB_TOKN", ["No Agent Secret is named"], id="unknown"),
        pytest.param(
            [_secret(), _token("GITHUB_TOKEN")],
            "github-tokn",
            ["Closest names: GITHUB_TOKEN"],
            id="close-name",
        ),
        pytest.param(
            [
                _secret(),
                _secret(id="sec_SESSAAAAAAAA", lifetime="session", session_id="ses_9", agents=[]),
            ],
            "GITHUB",
            ["names 2 Agent Secrets", "sec_SESSAAAAAAAA (session, Session ses_9)"],
            id="ambiguous",
        ),
    ],
)
def test_a_secret_name_that_does_not_resolve_lists_the_candidates(
    rpc: FakeRpc,
    run_cli: RunCli,
    secrets: list[dict[str, Any]],
    target: str,
    fragments: list[str],
) -> None:
    rpc.reply("agent_secrets.list", _overview(secrets))

    code, out, err = run_cli("agent-secrets", "reveal", target)

    assert code == 1 and rpc.methods == ["agent_secrets.list"]
    assert all(fragment in out + err for fragment in fragments)


def test_delete_succeeds_also_when_the_secret_is_already_gone(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("agent_secrets.list", _overview([_secret()])).reply("agent_secrets.list", _overview())
    rpc.reply("agent_secrets.delete", {"id": "sec_GITHAAAAAAAA", "deleted": True})

    deleted = run_cli("agent-secrets", "delete", "GITHUB")
    gone = run_cli("agent-secrets", "delete", "GITHUB")

    assert deleted[0] == 0 and deleted[1].startswith("Deleted the Agent Secret GITHUB")
    assert rpc.params("agent_secrets.delete") == {"id": "sec_GITHAAAAAAAA"}
    assert gone[0] == 0 and "Nothing was deleted." in gone[1] and "[WARN]" in gone[2]


def test_reveal_prints_only_the_value_and_refuses_inside_a_run(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc.reply("agent_secrets.list", _overview([_secret()])).reply(
        "agent_secrets.reveal", {"id": "sec_GITHAAAAAAAA", "field": "password", "value": _VALUE}
    )

    code, out, _err = run_cli("agent-secrets", "reveal", "GITHUB")

    assert code == 0 and out == f"{_VALUE}\n"
    assert rpc.params("agent_secrets.reveal") == {"id": "sec_GITHAAAAAAAA", "field": "password"}

    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "main")
    refused = run_cli("agent-secrets", "reveal", "GITHUB", "--field", "username")
    assert refused[0] == 1 and "Agent's Run" in refused[1] + refused[2]
    assert rpc.methods == ["agent_secrets.list", "agent_secrets.reveal"]


def test_answer_and_decline_settle_a_waiting_request(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdin(monkeypatch, _VALUE.encode())
    rpc.reply("agent_secrets.list", _overview(requests=[_REQUEST]))
    rpc.reply("agent_secrets.answer", {"secret": _token("NPM_TOKEN", lifetime="until")})
    rpc.reply("agent_secrets.decline", {"id": "sreq_K7QM4XPA", "declined": True})

    answered = run_cli("agent-secrets", "answer", "sreq_K7QM4XPA", "--value-stdin", "--for", "2h")
    declined = run_cli("agent-secrets", "decline", "npm-token")

    params = rpc.params("agent_secrets.answer")
    assert answered[0] == 0 and "continues with it" in answered[1]
    assert params["values"] == {"value": _VALUE} and params["lifetime"] == "until"
    assert params["expires_at"].endswith("+00:00")
    assert declined[0] == 0 and rpc.params("agent_secrets.decline") == {"id": "sreq_K7QM4XPA"}
    assert _VALUE not in answered[1] + answered[2]

    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "coder")
    refused = run_cli("agent-secrets", "decline", "sreq_K7QM4XPA")
    assert refused[0] == 1 and "Agent's Run" in refused[1] + refused[2]


def test_source_unlock_reads_each_unlock_field(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdin(monkeypatch, f"{_VALUE}\n\n".encode())
    rpc.reply("agent_secrets.list", _overview(sources=[_VAULT]))
    rpc.reply("agent_secrets.source_unlock", {**_VAULT, "state": "available"})

    code, out, err = run_cli("agent-secrets", "source", "unlock", "KeePassXC", "--value-stdin")

    assert code == 0 and out.startswith("Unlocked KeePassXC.")
    assert rpc.params("agent_secrets.source_unlock") == {
        "source": "keepassxc",
        "inputs": {"password": _VALUE},
    }
    assert _VALUE not in out + err


def test_source_entries_show_visible_fields_within_the_limit(rpc: FakeRpc, run_cli: RunCli) -> None:
    entries = [
        {
            "reference": f"entry-{index}",
            "title": f"Site {index}",
            "kind": "login",
            "container": "Work",
            "fields": [
                {"name": "username", "concealed": False, "value": "julian"},
                {"name": "password", "concealed": True},
            ],
        }
        for index in range(60)
    ]
    rpc.reply("agent_secrets.list", _overview(sources=[{**_VAULT, "state": "available"}]))
    rpc.reply(
        "agent_secrets.source_entries",
        {"source": "keepassxc", "containers": ["Work", "Home"], "entries": entries},
    )

    code, out, _err = run_cli("agent-secrets", "source", "entries", "keepassxc")

    lines = out.splitlines()
    assert code == 0
    assert lines[0] == "60 entries in KeePassXC; showing the first 50:"
    assert lines[1].startswith("entry-0 title=Site 0 kind=login container=Work")
    assert "fields=username:julian,password" in lines[1]
    assert len([line for line in lines if line.startswith("entry-")]) == 50
    assert lines[-1] == "Containers: Work, Home"


def test_add_takes_the_use_spellings_users_type(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stdin(monkeypatch, _VALUE.encode())
    rpc.reply("agent_secrets.list", _overview()).reply("agent_secrets.create", {"secret": _token()})

    code, _out, _err = run_cli("agent-secrets", "add", "KEY", "--sink", "env,File", "--value-stdin")

    assert code == 0 and rpc.params("agent_secrets.create")["sinks"] == ["environment", "files"]


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        pytest.param(["--sink", "terminl"], "Did you mean terminal?", id="sink-typo"),
        pytest.param(["--for", "0m"], "is no duration", id="zero-duration"),
        pytest.param(["--until", "2026-11-01"], "2026-11-01T23:59", id="date-without-time"),
        pytest.param(["--for", "2h", "--until", "2026-11-01T18:00"], "not allowed", id="both"),
    ],
)
def test_add_refuses_invalid_uses_and_expiries_as_usage_errors(
    rpc: FakeRpc,
    run_cli: RunCli,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    fragment: str,
) -> None:
    with pytest.raises(SystemExit) as done:
        run_cli("agent-secrets", "add", "OPENAI_KEY", *argv)

    assert done.value.code == 2 and fragment in capsys.readouterr().err
    assert rpc.calls == []


def test_the_parser_offers_the_kinds_and_sinks_of_the_secret_store() -> None:
    from core.secrets.agent_secrets import SECRET_KINDS, SINK_CLASSES

    assert tuple(SECRET_KINDS) == AGENT_SECRET_KINDS
    assert tuple(SINK_CLASSES) == AGENT_SECRET_SINKS
