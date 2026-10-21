"""Contract tests of the Permission Rules language: normalization, resolution, presets."""

from __future__ import annotations

import pytest

from core.permissions import (
    PermissionRule,
    PermissionRules,
    PermissionRulesError,
    apply_preset,
    check_permission_rules,
    describe_permission_rules,
    matching_preset,
    normalize_permission_rule,
    normalize_permission_rules,
    permission_rules_to_dict,
    preset_name,
    resolve_rule_set,
)


@pytest.mark.parametrize(
    ("platform", "written", "saved", "reported"),
    [
        pytest.param("linux", "Write(src/**)", "Edit(src/**)", True, id="write-alias"),
        pytest.param("linux", "MultiEdit(src/a.py)", "Edit(src/a.py)", True, id="multiedit"),
        pytest.param("linux", "NotebookEdit(a.ipynb)", "Edit(a.ipynb)", True, id="notebookedit"),
        pytest.param("linux", "Glob(docs/**)", "Read(docs/**)", True, id="glob-alias"),
        pytest.param("linux", "Grep(docs)", "Read(docs)", True, id="grep-alias"),
        pytest.param("linux", "LS(docs)", "Read(docs)", True, id="ls-alias"),
        pytest.param("linux", "List(docs)", "Read(docs)", True, id="list-alias"),
        pytest.param("linux", "Bash(git push *)", "Shell(git push *)", True, id="bash-alias"),
        pytest.param("linux", "PowerShell(Remove-Item *)", "Shell(Remove-Item *)", True, id="ps"),
        pytest.param(
            "linux", "WebFetch(domain:Example.COM)", "Web(example.com)", True, id="webfetch"
        ),
        pytest.param("linux", "Bash(npm run test:*)", "Shell(npm run test *)", True, id="prefix"),
        pytest.param("linux", "Bash", "Shell(*)", True, id="bare-command-kind"),
        pytest.param("linux", "WebFetch", "Web(*)", True, id="bare-web-kind"),
        pytest.param("linux", "Read(//etc/hosts)", "Read(/etc/hosts)", True, id="cc-absolute"),
        pytest.param("win32", "Read(//c/Users/me)", "Read(C:/Users/me)", True, id="cc-drive"),
        pytest.param(
            "linux", "read( {WorkDir}/./docs//a/ )", "Read({workdir}/docs/a)", False, id="spacing"
        ),
        pytest.param("linux", "Shell(  git   status )", "Shell(git status)", False, id="command"),
        pytest.param("linux", "Web(*.Example.com.)", "Web(*.example.com)", False, id="subdomains"),
        pytest.param("linux", "Web(bücher.de)", "Web(xn--bcher-kva.de)", False, id="idna"),
        pytest.param("linux", "Web([::1])", "Web(::1)", False, id="ip-literal"),
        pytest.param(
            "linux",
            "Edit({project}/../shared/**)",
            "Edit({project}/../shared/**)",
            False,
            id="dotdot-after-placeholder",
        ),
        pytest.param("linux", "Read(~/notes)", "Read(~/notes)", False, id="tilde"),
        pytest.param("linux", "Read(/srv/a/../b)", "Read(/srv/b)", False, id="dotdot"),
        pytest.param(
            "win32", "Read(c:\\Users\\me\\Docs\\**)", "Read(C:/Users/me/Docs/**)", False, id="win"
        ),
        pytest.param(
            "win32", "Read(\\\\?\\C:\\x\\y. )", "Read(C:/x/y)", False, id="win-verbatim-prefix"
        ),
    ],
)
def test_rule_strings_normalize_to_their_canonical_form(
    platform: str, written: str, saved: str, reported: bool
) -> None:
    outcome = normalize_permission_rule(written, platform=platform)

    assert outcome.error is None
    assert outcome.rule is not None and outcome.rule.text == saved
    assert (outcome.note is not None) is reported


@pytest.mark.parametrize(
    ("written", "named"),
    [
        pytest.param("Foo(x)", "Foo", id="unknown-kind"),
        pytest.param("Read()", "Read", id="path-kind-without-pattern"),
        pytest.param("Read({wrkdir}/x)", "{wrkdir}", id="unknown-placeholder"),
        pytest.param("Read(docs/{workdir})", "{workdir}", id="placeholder-not-first"),
        pytest.param("Read({workdir}x)", "{workdir}", id="placeholder-without-slash"),
        pytest.param("Read(a\x01b)", "control", id="control-character"),
        pytest.param("Web(example.com:8080)", "port", id="host-with-port"),
        pytest.param("Web(ex*ample.com)", "*", id="inner-wildcard"),
        pytest.param("Web(*.10.0.0.1)", "IP", id="wildcard-ip"),
        pytest.param("Web(exa mple.com)", "host", id="host-with-space"),
        pytest.param("Shell(git\nstatus)", "line", id="multiline-command"),
        pytest.param("Read docs", "Kind(pattern)", id="no-parentheses"),
    ],
)
def test_malformed_rule_strings_are_refused_with_the_reason(written: str, named: str) -> None:
    outcome = normalize_permission_rule(written, platform="linux")

    assert outcome.rule is None
    assert outcome.error is not None and named in outcome.error


def test_a_drive_relative_pattern_is_refused_on_windows() -> None:
    outcome = normalize_permission_rule("Read(C:docs)", platform="win32")

    assert outcome.rule is None and outcome.error is not None


@pytest.mark.parametrize(
    ("value", "named"),
    [
        pytest.param([], "object", id="not-an-object"),
        pytest.param({"reed": "work_area"}, "reed", id="unknown-part"),
        pytest.param({"read": "somewhere"}, "work_area", id="unknown-read-base"),
        pytest.param({"write": "project"}, "scratch", id="unknown-write-base"),
        pytest.param({"commands": True}, "listed", id="base-of-wrong-type"),
        pytest.param({"allow": "Read(docs)"}, "list", id="rules-not-a-list"),
        pytest.param({"deny": ["Read(docs)", 3]}, "deny[1]", id="rule-not-a-string"),
        pytest.param({"deny": ["Read(docs)", "Copy(x)"]}, "deny[1]", id="rule-error-located"),
    ],
)
def test_invalid_rule_sets_are_refused_naming_the_part(value: object, named: str) -> None:
    with pytest.raises(PermissionRulesError) as raised:
        normalize_permission_rules(value)

    # The editors' check reports the same problem without raising.
    check = check_permission_rules(value)
    assert named in str(raised.value)
    assert check.rules is None
    assert [problem.message for problem in check.problems] == [str(raised.value)]


def test_the_check_reports_every_problem_of_a_rule_set() -> None:
    check = check_permission_rules(
        {"read": "somewhere", "allow": ["Copy(x)", "Read(docs)", 3], "deny": "Read(docs)"}
    )

    assert [(problem.part, problem.index, problem.rule) for problem in check.problems] == [
        ("read", None, None),
        ("allow", 0, "Copy(x)"),
        ("allow", 2, None),
        ("deny", None, None),
    ]
    assert "allow[2]" in check.problems[2].message


def test_the_stored_form_round_trips_and_reports_rewritten_rules() -> None:
    notes: list[str] = []
    stored = {
        "read": " Work_Area ",
        "write": "scratch",
        "commands": "listed",
        "web": "any",
        "allow": ["Bash(git status)", "Shell(git status)", "Edit({scratch}/out/**)"],
        "deny": [],
    }

    rules = normalize_permission_rules(stored, notes=notes, platform="linux")
    document = permission_rules_to_dict(rules)

    assert document == {
        "read": "work_area",
        "write": "scratch",
        "commands": "listed",
        "web": "any",
        "allow": ["Shell(git status)", "Edit({scratch}/out/**)"],
        "deny": [],
    }
    assert len(notes) == 1
    assert normalize_permission_rules(document, platform="linux") == rules
    assert permission_rules_to_dict(normalize_permission_rules({})) == {}
    assert normalize_permission_rules({"read": None}) == PermissionRules()


def _rules(**parts: object) -> PermissionRules:
    return normalize_permission_rules(parts, platform="linux")


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        pytest.param((None, None), None, id="no-rule-set-is-full-access"),
        pytest.param((None, _rules()), PermissionRules(), id="empty-set-is-restricted"),
        pytest.param(
            (_rules(read="work_area"), _rules(read="anywhere", write="scratch")),
            PermissionRules(read="work_area", write="scratch"),
            id="first-level-wins-per-part",
        ),
        pytest.param(
            (_rules(allow=[]), _rules(allow=["Read(/docs)"], deny=["Shell(rm *)"])),
            PermissionRules(allow=(), deny=(PermissionRule("Shell", "rm *"),)),
            id="an-empty-list-is-set",
        ),
    ],
)
def test_rule_sets_resolve_per_part(
    levels: tuple[PermissionRules | None, ...], expected: PermissionRules | None
) -> None:
    assert resolve_rule_set(*levels) == expected


@pytest.mark.parametrize("written", ["work-dir", "work_dir", " Work dir "])
def test_a_preset_sets_the_bases_and_keeps_the_rules(written: str) -> None:
    custom = _rules(commands="listed", allow=["Shell(npm test)"], deny=["Read(**/.env)"])

    applied = apply_preset(preset_name(written), custom)

    assert applied == PermissionRules(
        read="work_area", write="work_area", allow=custom.allow, deny=custom.deny
    )
    assert (matching_preset(applied), matching_preset(custom)) == ("work_dir", "custom")
    assert apply_preset("full_access", custom) is None
    with pytest.raises(PermissionRulesError, match="full-access, work-dir or work-dir-read-only"):
        preset_name("everything")


def test_a_description_expands_placeholders_and_lists_inactive_rules() -> None:
    rules = _rules(
        read="work_area",
        write="scratch",
        allow=["Edit(build/**)", "Read({project}/docs)", "Shell(npm test)"],
        deny=["Read(~/.ssh/**)"],
    )

    described = describe_permission_rules(
        rules,
        values={"workdir": "/srv/agent", "project": None, "home": "/home/me"},
        platform="linux",
    )

    assert described == {
        "source": "agent",
        "bases": {"read": "work_area", "write": "scratch", "commands": "any", "web": "any"},
        "workdir": "/srv/agent",
        "read": ["/srv/agent", "{scratch}", "/srv/agent/build/**"],
        "write": ["{scratch}", "/srv/agent/build/**"],
        "commands": None,
        "web": None,
        "deny": ["Read(/home/me/.ssh/**)"],
        # Without a Working Project, ``{project}`` has no value and the rule is dropped.
        "inactive": ["Read({project}/docs)"],
    }
