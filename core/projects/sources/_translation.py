"""Adapter helpers that map what a foreign Agent may do onto whole vBot Tools."""

from __future__ import annotations

from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from core.permissions import normalize_permission_rule, normalize_permission_rules
from core.projects.paths import slugify_agent_id
from core.projects.sources._reading import SourceError, names, string
from core.projects.sources.profile import (
    AgentProfile,
    AgentTargetRule,
    ToolRule,
    Translation,
    TranslationStatus,
)
from core.settings import validate_temperature, validate_thinking_effort, validate_top_p

# A foreign shell denial also closes vBot's independent interactive shell.
SHELL_TOOLS = frozenset({"bash", "terminal"})
READ_ONLY_TOOLS = frozenset({"read", "search_files", "web_fetch", "web_search", "status"})


def profile(
    source: str,
    path: Path,
    fields: dict[str, Any],
    body: str,
    *,
    name: str | None = None,
    effort_key: str = "effort",
    model_default: str = "",
) -> AgentProfile:
    raw_name = (
        name if name is not None else string(fields, "name", path.stem.removesuffix(".agent"))
    )
    agent_id = slugify_agent_id(raw_name)
    translations = [
        Translation("instructions", "applied", "Repository instructions are included verbatim.")
    ]
    raw_model = fields.get("model", model_default)
    if isinstance(raw_model, list) and raw_model and isinstance(raw_model[0], str):
        # A prioritized list names the preferred Model first.
        fields = {**fields, "model": raw_model[0]}
    model = string(fields, "model", model_default)
    if model:
        translations.append(
            Translation(
                "model", "translated", "Resolved through the Project Model mapping or defaults."
            )
        )
    effort = None
    if effort_key in fields:
        try:
            effort = validate_thinking_effort(
                fields[effort_key], label=effort_key, allow_none=False
            )
        except ValueError:
            # A foreign effort level vBot lacks is a lost wish, not broken metadata.
            translations.append(
                Translation(effort_key, "not_supported", "No matching vBot thinking effort.")
            )
        else:
            translations.append(Translation(effort_key, "translated", "Used as thinking effort."))
    temperature = None
    if "temperature" in fields:
        temperature = validate_temperature(
            fields["temperature"], label="temperature", allow_none=False
        )
        translations.append(Translation("temperature", "applied", "Used as sampling temperature."))
    top_p = None
    if "top_p" in fields:
        top_p = validate_top_p(fields["top_p"], label="top_p", allow_none=False)
        translations.append(Translation("top_p", "applied", "Used as top_p."))
    return AgentProfile(
        agent_id=agent_id,
        display_name=raw_name,
        description=string(fields, "description"),
        model=model,
        temperature=temperature,
        top_p=top_p,
        body=body,
        source=source,
        source_path=path,
        thinking_effort=effort,
        translations=tuple(translations),
    )


def unavailable(source: str, path: Path, name: str, reason: str) -> AgentProfile:
    try:
        agent_id = slugify_agent_id(name)
    except ValueError:
        # Structural failures retain a row, but cannot become an addressable Agent.
        agent_id = ""
    return AgentProfile(
        agent_id=agent_id,
        display_name=name,
        description="",
        model="",
        temperature=None,
        body="",
        source=source,
        source_path=path,
        allowed_tools=frozenset(),
        unavailable_reason=reason,
        translations=(Translation("metadata", "not_supported", reason),),
    )


def unsupported(
    agent: AgentProfile,
    fields: dict[str, Any],
    known: set[str],
    cosmetic: frozenset[str] = frozenset({"color"}),
) -> AgentProfile:
    reports = list(agent.translations)
    for key in fields:
        if key in known or key in cosmetic:
            continue
        reports.append(Translation(key, "not_supported", "This setting has no vBot equivalent."))
    return replace(agent, translations=tuple(reports))


def restrict(agent: AgentProfile, setting: str, tools: frozenset[str], detail: str) -> AgentProfile:
    """Remove Tools for a capability the foreign Agent does not have at all."""
    return replace(
        agent,
        denied_tools=agent.denied_tools | tools,
        translations=(*agent.translations, Translation(setting, "translated", detail)),
    )


def tool_list(
    agent: AgentProfile,
    value: Any,
    mapping: dict[str, frozenset[str]],
    *,
    deny: bool = False,
    setting: str = "tools",
    wildcard: bool = False,
    translated_scopes: frozenset[str] = frozenset(),
) -> AgentProfile:
    """Map a foreign Tool list onto vBot Tools.

    vBot grants whole Tools: an entry the Agent may use for some commands or paths
    grants the Tool, and only an unscoped denial removes it. The scopes of the
    entries named in ``translated_scopes`` become Permission Rules, which the
    caller translates and reports.
    """
    entries = names(value, comma_separated=True)
    targets: list[AgentTargetRule] = list(agent.agent_target_rules)
    reports = list(agent.translations)
    allowed: set[str] = set()
    denied: set[str] = set(agent.denied_tools)
    unrestricted_targets = False
    inherit_selection = False
    scoped_targets: list[str] = []
    for entry in entries:
        base, separator, rest = entry.strip().lower().partition("(")
        base = "agent" if base == "task" else base
        if base not in mapping and "/" in base:
            # Qualified names (``read/readFile``) belong to their Tool set.
            base = base.partition("/")[0]
        if wildcard and base == "*" and not separator:
            if deny:
                return replace(agent, tool_rules=(*agent.tool_rules, ToolRule("*", False)))
            inherit_selection = True
            unrestricted_targets = True
            continue
        mapped = mapping.get(base)
        if mapped is None:
            reports.append(
                Translation(f"{setting}.{entry}", "not_supported", "No matching vBot Tool.")
            )
            continue
        if separator:
            if not rest.endswith(")"):
                raise SourceError("Malformed Tool scope.")
            if base == "agent":
                patterns = [part.strip() for part in rest[:-1].split(",") if part.strip()]
                if not patterns:
                    raise SourceError("Agent scope must name at least one target.")
                if deny:
                    targets.extend(AgentTargetRule(pattern, False) for pattern in patterns)
                else:
                    scoped_targets.extend(patterns)
                    allowed.update(mapped)
                continue
            if not deny:
                allowed.update(mapped)
            if base not in translated_scopes:
                reports.append(
                    Translation(
                        f"{setting}.{entry}",
                        "translated",
                        "The whole vBot Tool "
                        + ("stays available" if deny else "is granted")
                        + "; its command or path scope is not translated.",
                    )
                )
        elif deny:
            denied.update(mapped)
            if "bash" in mapped:
                denied.update(SHELL_TOOLS)
            if base == "agent":
                targets.append(AgentTargetRule("*", False))
        else:
            allowed.update(mapped)
            unrestricted_targets |= base == "agent"
    if not deny and not unrestricted_targets:
        targets.append(AgentTargetRule("*", False))
        targets.extend(AgentTargetRule(pattern, True) for pattern in scoped_targets)
    reports.append(
        Translation(
            setting,
            "translated",
            "Mapped to an exact vBot Tool selection."
            if not deny
            else "Mapped to vBot Tool denials.",
        )
    )
    return replace(
        agent,
        allowed_tools=agent.allowed_tools
        if deny or inherit_selection
        else frozenset(allowed - denied),
        denied_tools=frozenset(denied),
        agent_target_rules=tuple(targets),
        translations=tuple(reports),
    )


class RuleDraft:
    """Permission Rules an adapter translates from a repository's own permission settings.

    The result restricts only when the repository restricts: a narrowed base or
    a deny rule. Allow rules that only widen (``additionalDirectories``, extra
    writable roots) join such a rule set and are dropped otherwise, because an
    unrestricted Agent needs none. Each translated or untranslatable setting adds
    a row to the Profile's translation report.
    """

    def __init__(self) -> None:
        self._bases: dict[str, str] = {}
        self._allow: list[str] = []
        self._deny: list[str] = []
        self._widening: list[tuple[str, str]] = []
        self.reports: list[Translation] = []

    def narrow(self, part: str, value: str, setting: str, detail: str) -> None:
        """Set a base, keeping the narrower one when two settings set it."""
        order = _BASE_ORDER[part]
        current = self._bases.get(part)
        if current is None or order.index(value) < order.index(current):
            self._bases[part] = value
        self.report(setting, "translated", detail)

    def rule(self, effect: str, text: str, setting: str, *, detail: str = "") -> bool:
        """Add one allow or deny rule string; an invalid one is reported, not added."""
        outcome = normalize_permission_rule(text)
        if outcome.rule is None:
            reason = (outcome.error or "").removeprefix(f'"{text.strip()}": ')
            self.report(setting, "not_supported", f"Not translated: {reason}")
            return False
        target = self._allow if effect == "allow" else self._deny
        if outcome.rule.text not in target:
            target.append(outcome.rule.text)
        self.report(
            setting,
            "translated",
            f"Translated to the {effect} rule {outcome.rule.text}."
            + (f" {detail}" if detail else ""),
        )
        return True

    def widen(self, text: str, setting: str) -> None:
        """An allow rule that matters only beside a restriction (see the class)."""
        self._widening.append((text, setting))

    def report(self, setting: str, status: TranslationStatus, detail: str) -> None:
        self.reports.append(Translation(setting, status, detail))

    def apply(self, agent: AgentProfile) -> AgentProfile:
        restricts = bool(self._deny) or any(
            value != _BASE_ORDER[part][-1] for part, value in self._bases.items()
        )
        for text, setting in self._widening:
            if restricts:
                self.rule("allow", text, setting)
            else:
                self.report(
                    setting,
                    "translated",
                    "Needs no rule: the repository does not otherwise restrict this Agent.",
                )
        rules = None
        if restricts:
            # A base at full access would replace a narrower vBot default.
            document: dict[str, Any] = {
                part: value for part, value in self._bases.items() if value != _BASE_ORDER[part][-1]
            }
            if self._allow:
                document["allow"] = self._allow
            if self._deny:
                document["deny"] = self._deny
            rules = normalize_permission_rules(document)
        return replace(
            agent,
            permission_rules=rules,
            translations=(*agent.translations, *self.reports),
        )


# Bases from the narrowest to full access.
_BASE_ORDER: dict[str, tuple[str, ...]] = {
    "read": ("work_area", "anywhere"),
    "write": ("scratch", "work_area", "anywhere"),
    "commands": ("listed", "any"),
    "web": ("listed", "any"),
}

ASK_DETAIL = (
    "Not translated: rules that only ask first still let the Agent act, since vBot asks "
    "no approvals."
)


def permission_rules(value: Any) -> tuple[AgentTargetRule, ...]:
    if isinstance(value, str):
        entries = {"*": value}
    elif isinstance(value, dict):
        entries = value
    else:
        raise SourceError("Permission must be an action or pattern/action object.")
    if not entries:
        raise SourceError("Permission rules cannot be empty.")
    result: list[AgentTargetRule] = []
    for pattern, action in entries.items():
        if (
            not isinstance(pattern, str)
            or not isinstance(action, str)
            or action not in {"allow", "deny", "ask"}
        ):
            raise SourceError(
                "Permission rules require string patterns and allow/deny/ask actions."
            )
        result.append(AgentTargetRule(pattern, action != "deny"))
    return tuple(result)


def rules_allow(rules: tuple[AgentTargetRule, ...], name: str, default: bool = True) -> bool:
    result = default
    for rule in rules:
        if fnmatchcase(name, rule.pattern):
            result = rule.allowed
    return result
