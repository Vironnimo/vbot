// Pure view helpers for the Projects tab. Business and normalization logic
// lives here so the Svelte component stays a thin display/input/orchestration
// layer (see webui.md → Conventions). Every export is unit-tested in
// __tests__/projectsView.test.js.
//
// The shapes mirror the verified backend contract (server/rpc/project_methods):
//   project: { project_id, display_name, cwd, cwd_exists, default_agent,
//              default_model, auto_load[], created_at, updated_at }
//   scan:    { team: [member…], report: { clean, findings: [finding…] } }
//   finding: { type, detail, agent_id, source_path }

// The scan report's `finding.type` discriminants (server scan_report.py).
export const FINDING_TYPE_SLUG_COLLISION = 'slug_collision';
export const FINDING_TYPE_UNSLUGIFIABLE_NAME = 'unslugifiable_name';
export const FINDING_TYPE_BAD_MODEL = 'bad_model';
export const FINDING_TYPE_ORPHAN = 'orphan';

// Stable display order for grouped findings, so the report always lists the
// same finding kinds in the same order regardless of server ordering.
const FINDING_TYPES = Object.freeze([
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
]);

// The mutable fields a manage form can change through project.set. cwd is
// handled by the dedicated re-point path, and default_temperature /
// default_thinking_effort have their own typed diff (number/null and
// null/''/level), so they are not part of this generic string-trim diff.
const MANAGE_FIELDS = Object.freeze([
  'display_name',
  'default_agent',
  'default_model',
  'source_format',
]);

// Fields in the generic diff that are required non-empty on the backend: an
// empty form value is "no change", never a clear-to-null.
const NON_CLEARABLE_MANAGE_FIELDS = Object.freeze(
  new Set(['display_name', 'source_format']),
);

// The per-project source format vocabulary (mirrors the backend
// PROJECT_SOURCE_FORMATS): which coding-agent ecosystem the project's Team
// agents and skills come from. Exactly one per project — no mixing.
export const PROJECT_SOURCE_FORMATS = Object.freeze(['opencode', 'claude']);
export const DEFAULT_PROJECT_SOURCE_FORMAT = 'opencode';

// The list-valued whitelist fields, diffed by SET (order-insensitive) so a
// reorder alone never counts as a change. Tool/skill names are unordered membership
// sets; an empty list is a real value (e.g. every tool off).
const WHITELIST_LIST_FIELDS = Object.freeze([
  'allowed_tools',
  'skills_bundled_enabled',
  'skills_global_enabled',
  'skills_project_disabled',
]);

// Tools that are never part of a project Tool Whitelist, so the editor hides them from
// the toggle catalog: `memory` is runtime-derived from the agent's memory mode, and
// `skill_manage` is identity-only (it authors into an identity agent's private skill
// home — a project/config agent never owns one). `skill` itself stays a normal,
// toggleable project tool.
export const PROJECT_TOOL_WHITELIST_EXCLUDED = Object.freeze([
  'memory',
  'skill_manage',
]);

// The dropdown sentinel for "no project default" thinking effort. Defined here
// (not imported from settingsView.js) to keep the two view modules decoupled; it
// mirrors AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT. Distinct from '' which is a
// real value meaning "provider default" (stops the resolution chain).
export const PROJECT_THINKING_EFFORT_NO_DEFAULT =
  '__project_thinking_effort_no_default__';

// The effort ladder a project default may pick (mirrors the agent thinking
// levels). The sentinel and '' (provider default) are added around these in the
// dropdown; only these literals are accepted as a real level in the payload.
export const PROJECT_THINKING_EFFORT_OPTIONS = Object.freeze([
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]);

// Build the project.add payload from the add-form values. cwd is required (the
// thin api wrapper enforces it too); the optional pointers are only included
// when the user actually typed something, matching the backend's
// "non-empty string" rule for these params.
export function buildAddProjectPayload(formValues) {
  const payload = {
    cwd: asText(formValues?.cwd).trim(),
  };

  const displayName = optionalText(formValues?.display_name);
  if (displayName !== null) {
    payload.display_name = displayName;
  }

  const defaultAgent = optionalText(formValues?.default_agent);
  if (defaultAgent !== null) {
    payload.default_agent = defaultAgent;
  }

  const defaultModel = optionalText(formValues?.default_model);
  if (defaultModel !== null) {
    payload.default_model = defaultModel;
  }

  // Only send an explicit, known format — absent means the server auto-detects
  // from the repo (exactly one format present → that one, else opencode).
  const sourceFormat = asText(formValues?.source_format).trim();
  if (PROJECT_SOURCE_FORMATS.includes(sourceFormat)) {
    payload.source_format = sourceFormat;
  }

  // Only include the knobs when the form carries a real value: a number for
  // temperature, and a level or '' (provider default) for thinking effort. The
  // "no default" sentinel / empty temperature box means "omit" at add time.
  const defaultTemperature = normalizeProjectTemperature(
    formValues?.default_temperature,
  );
  if (defaultTemperature !== null) {
    payload.default_temperature = defaultTemperature;
  }

  const defaultThinkingEffort = normalizeProjectThinkingEffortForPayload(
    formValues?.default_thinking_effort,
  );
  if (defaultThinkingEffort !== null) {
    payload.default_thinking_effort = defaultThinkingEffort;
  }

  const autoLoad = normalizeAutoLoad(formValues?.auto_load);
  if (autoLoad.length > 0) {
    payload.auto_load = autoLoad;
  }

  return payload;
}

// Build the sparse project.set changes for a manage form: only fields whose
// value actually differs from the current project, and at least one (callers
// must guard with `hasManageChanges` before sending). project.set rejects an
// empty change set, so this never produces one silently — an unchanged form
// yields `{}` and the caller short-circuits.
//
// auto_load is compared as an ordered list; display_name / default_agent /
// default_model compare as trimmed strings. A pointer field
// (default_agent/default_model) cleared to empty is sent as `null` — the
// backend's `_optional_string` rejects a sent empty string with
// `invalid_request`, and only maps JSON `null` (None) to "" to clear the
// pointer (fall through the model chain). A non-empty pointer is sent as the
// trimmed string. display_name cannot be cleared (it is a required non-empty
// field) so an empty display_name is treated as no change.
export function buildManageProjectPayload(formValues, project) {
  const changes = {};

  for (const field of MANAGE_FIELDS) {
    const next = asText(formValues?.[field]).trim();
    const current = asText(project?.[field]).trim();
    if (next === current) {
      continue;
    }
    if (NON_CLEARABLE_MANAGE_FIELDS.has(field) && next === '') {
      // Required non-empty on the backend; an empty box is not a clear.
      continue;
    }
    // A cleared pointer must be sent as null (the backend maps None → "" to
    // clear it); a sent empty string would be rejected as invalid_request.
    changes[field] = next === '' ? null : next;
  }

  // Temperature: form string → number|null; send only when it differs from the
  // stored value. null clears the project default (fall through the chain), a
  // number sets it (0 is a real value, the sampling floor).
  const nextTemperature = normalizeProjectTemperature(
    formValues?.default_temperature,
  );
  const currentTemperature = numberOrNull(project?.default_temperature);
  if (nextTemperature !== currentTemperature) {
    changes.default_temperature = nextTemperature;
  }

  // Thinking effort: form (sentinel|''|level) → null|''|level; send only on a
  // change. null clears the project default, '' forces the provider default, a
  // level sets it.
  const nextThinkingEffort = normalizeProjectThinkingEffortForPayload(
    formValues?.default_thinking_effort,
  );
  const currentThinkingEffort = stringOrNull(project?.default_thinking_effort);
  if (nextThinkingEffort !== currentThinkingEffort) {
    changes.default_thinking_effort = nextThinkingEffort;
  }

  const nextAutoLoad = normalizeAutoLoad(formValues?.auto_load);
  const currentAutoLoad = normalizeAutoLoad(project?.auto_load);
  if (!sameStringList(nextAutoLoad, currentAutoLoad)) {
    changes.auto_load = nextAutoLoad;
  }

  // The Tool/Skill Whitelist lists are membership sets: send a field only when its
  // set actually changed, so toggling tools/skills persists but a mere reorder does
  // not. An empty list (e.g. every tool off) is a real value and is sent as `[]`.
  for (const field of WHITELIST_LIST_FIELDS) {
    const next = normalizeStringList(formValues?.[field]);
    const current = normalizeStringList(project?.[field]);
    if (!sameStringSet(next, current)) {
      changes[field] = next;
    }
  }

  return changes;
}

// Build the tool toggle rows for the editor: every catalog tool (minus the tools
// excluded from a project whitelist — see `PROJECT_TOOL_WHITELIST_EXCLUDED`) with
// whether it is in the project's current Tool Whitelist. The catalog is the
// tool-catalog RPC's tool list, so new tools appear automatically. Rows are sorted by
// name for a stable display. Each row carries the tool's readiness fields
// (`ready`/`readiness_hint`/`extension`) so a not-ready tool renders the shared
// "currently unavailable" notice (its toggle stays functional — the whitelist is
// independent of readiness). A string catalog entry has no readiness metadata, so
// it defaults to ready.
export function buildToolToggleList({ catalog = [], allowedTools = [] } = {}) {
  const excluded = new Set(PROJECT_TOOL_WHITELIST_EXCLUDED);
  const enabled = new Set(normalizeStringList(allowedTools));
  const byName = new Map();
  for (const tool of Array.isArray(catalog) ? catalog : []) {
    const isObject = tool !== null && typeof tool === 'object';
    const name = asText(isObject ? tool?.name : tool).trim();
    if (name.length === 0 || excluded.has(name) || byName.has(name)) {
      continue;
    }
    byName.set(name, {
      name,
      enabled: enabled.has(name),
      ready: isObject ? tool.ready !== false : true,
      readiness_hint: isObject ? (tool.readiness_hint ?? null) : null,
      extension: isObject ? (tool.extension ?? null) : null,
    });
  }
  return Array.from(byName.values()).sort((left, right) =>
    left.name.localeCompare(right.name),
  );
}

// Build the skill toggle sections for the editor from a project's skill pool and
// its stored whitelist rule. Project skills are on by default (off only when named
// in `skills_project_disabled`); bundled and global skills are off by default (on
// only when named in `skills_bundled_enabled` / `skills_global_enabled`). A bundled
// or global skill shadowed by a project skill of the same name is dropped from its
// section (project wins).
export function buildSkillToggleSections({
  projectSkills = [],
  bundledSkills = [],
  globalSkills = [],
  skillsBundledEnabled = [],
  skillsGlobalEnabled = [],
  skillsProjectDisabled = [],
} = {}) {
  const disabled = new Set(normalizeStringList(skillsProjectDisabled));
  const enabledBundled = new Set(normalizeStringList(skillsBundledEnabled));
  const enabledGlobal = new Set(normalizeStringList(skillsGlobalEnabled));
  const projectNames = normalizeStringList(projectSkills);
  const projectSet = new Set(projectNames);
  return {
    project: projectNames.map((name) => ({
      name,
      enabled: !disabled.has(name),
    })),
    bundled: normalizeStringList(bundledSkills)
      .filter((name) => !projectSet.has(name))
      .map((name) => ({ name, enabled: enabledBundled.has(name) })),
    global: normalizeStringList(globalSkills)
      .filter((name) => !projectSet.has(name))
      .map((name) => ({ name, enabled: enabledGlobal.has(name) })),
  };
}

// Add or remove a name from a list (returns a new normalized list), the single
// primitive the editor's toggle handlers use to mutate a whitelist field.
export function setListMembership(list, name, include) {
  const normalized = normalizeStringList(list);
  const target = asText(name).trim();
  if (!target) {
    return normalized;
  }
  const has = normalized.includes(target);
  if (include && !has) {
    return [...normalized, target];
  }
  if (!include && has) {
    return normalized.filter((item) => item !== target);
  }
  return normalized;
}

// Normalize the scan response's skill pool into the editor's name lists.
export function normalizeScanSkills(scan) {
  const skills = scan?.skills ?? {};
  return {
    project: normalizeStringList(skills.project),
    bundled: normalizeStringList(skills.bundled),
    global: normalizeStringList(skills.global),
  };
}

// Whether a manage payload carries at least one change (project.set needs ≥1).
export function hasManageChanges(changes) {
  return isPlainObject(changes) && Object.keys(changes).length > 0;
}

// Build the option list for a project's default-agent dropdown from the scanned
// team. The leading empty option (value '') is "no project default — fall
// through the resolution chain". A stored default_agent that is no longer in the
// team is kept as a trailing option so the current value stays visible and
// selectable rather than silently dropping when the team changes.
export function buildDefaultAgentOptions({
  team = [],
  currentValue = '',
  emptyLabel = '',
  unavailableLabel = (agentId) => agentId,
} = {}) {
  const current = asText(currentValue).trim();
  const options = [{ value: '', label: emptyLabel }];
  const seen = new Set();

  for (const member of Array.isArray(team) ? team : []) {
    const agentId = asText(member?.agent_id).trim();
    if (!agentId || seen.has(agentId)) {
      continue;
    }
    seen.add(agentId);
    const displayName = asText(member?.display_name).trim() || agentId;
    options.push({
      value: agentId,
      label: displayName,
      secondaryLabel: displayName === agentId ? '' : agentId,
    });
  }

  if (current && !seen.has(current)) {
    options.push({ value: current, label: unavailableLabel(current) });
  }

  return options;
}

// Normalize a project.detect response into the add dialog's stable shape:
// per-format `{agents, skills, present}` (present = ≥1 agent OR ≥1 skill — the
// creation-time detection rule) plus the context-file facts. A missing/foreign
// response degrades to "nothing found".
export function normalizeDetectResult(result) {
  const rawFormats = isPlainObject(result?.formats) ? result.formats : {};
  const formats = {};
  for (const key of PROJECT_SOURCE_FORMATS) {
    const entry = isPlainObject(rawFormats[key]) ? rawFormats[key] : {};
    const agents = countOrZero(entry.agents);
    const skills = countOrZero(entry.skills);
    formats[key] = { agents, skills, present: agents > 0 || skills > 0 };
  }
  const context = isPlainObject(result?.context_files)
    ? result.context_files
    : {};
  return {
    cwd_exists: result?.cwd_exists === true,
    formats,
    agents_md: context.agents_md === true,
    claude_md: optionalText(context.claude_md),
  };
}

// The formats a detect result found present, in canonical order. Drives the add
// dialog's three states: none → silent opencode default, one → quiet "Detected"
// line, both → the informed radio choice.
export function presentFormats(detect) {
  return PROJECT_SOURCE_FORMATS.filter(
    (key) => detect?.formats?.[key]?.present === true,
  );
}

// The CLAUDE.md comfort suggestion (decision 4): offer adding the found
// CLAUDE.md as a normal project file only when the repo has no AGENTS.md —
// an explicit user opt-in checkbox, nothing automatic.
export function shouldSuggestClaudeMd(detect) {
  return Boolean(detect?.claude_md) && detect?.agents_md !== true;
}

// A project's cwd no longer resolves to a directory → offer Re-Point. The flag
// is server-computed (`cwd_exists`); only an explicit `false` triggers it, so a
// missing/undefined flag never forces the re-point UI.
export function needsRePoint(project) {
  return project?.cwd_exists === false;
}

// The change set for a Re-Point: project.set with the new cwd only. The caller
// passes the project_id separately to setProject, so this is just `{ cwd }`.
export function buildRePointPayload(cwd) {
  return { cwd: asText(cwd).trim() };
}

// Normalize one project record from the backend into a stable display shape.
export function normalizeProject(project) {
  return {
    project_id: asText(project?.project_id),
    display_name: asText(project?.display_name),
    cwd: asText(project?.cwd),
    cwd_exists: project?.cwd_exists === true,
    default_agent: asText(project?.default_agent),
    default_model: asText(project?.default_model),
    default_temperature: numberOrNull(project?.default_temperature),
    default_thinking_effort: stringOrNull(project?.default_thinking_effort),
    source_format: PROJECT_SOURCE_FORMATS.includes(project?.source_format)
      ? project.source_format
      : DEFAULT_PROJECT_SOURCE_FORMAT,
    auto_load: normalizeAutoLoad(project?.auto_load),
    allowed_tools: normalizeStringList(project?.allowed_tools),
    skills_bundled_enabled: normalizeStringList(
      project?.skills_bundled_enabled,
    ),
    skills_global_enabled: normalizeStringList(project?.skills_global_enabled),
    skills_project_disabled: normalizeStringList(
      project?.skills_project_disabled,
    ),
    created_at: optionalText(project?.created_at),
    updated_at: optionalText(project?.updated_at),
  };
}

export function normalizeProjects(projects) {
  const raw = Array.isArray(projects) ? projects : [];
  return raw.map((project) => normalizeProject(project));
}

// The three per-agent overridable / effective run fields, in display order. Each is
// resolved through the config-agent chain (override → agent file → project default →
// global default) and reported by the scan as `effective[field] = {value, source}`.
export const TEAM_EFFECTIVE_FIELDS = Object.freeze([
  'model',
  'temperature',
  'thinking_effort',
]);

// The winning-source discriminants the scan reports on `effective[field].source`.
export const EFFECTIVE_SOURCE_OVERRIDE = 'override';
export const EFFECTIVE_SOURCE_AGENT = 'agent';
export const EFFECTIVE_SOURCE_PROJECT_DEFAULT = 'project_default';
export const EFFECTIVE_SOURCE_GLOBAL_DEFAULT = 'global_default';

// Project the scan's team into a stable, display-ready list. The repo is the
// source of truth (no copy drift) — this only shapes what the view renders. Each
// member carries its raw repo-declared values (for reference), the per-agent
// `overrides` object (or null), and the `effective` map of `{value, source}` per
// run field so the row can show the resolved value with provenance.
//
// NOTE: `agent_id` and `display_name` are consumed by ChatView's project team bar
// (the second consumer of this helper) — do not drop or rename them.
export function projectTeam(scan) {
  const raw = Array.isArray(scan?.team) ? scan.team : [];
  return raw.map((member) => ({
    agent_id: asText(member?.agent_id),
    display_name: asText(member?.display_name) || asText(member?.agent_id),
    description: asText(member?.description),
    model: asText(member?.model),
    temperature:
      typeof member?.temperature === 'number' ? member.temperature : null,
    thinking_effort: stringOrNull(member?.thinking_effort),
    source_format: asText(member?.source_format),
    source_path: asText(member?.source_path),
    denied_tools: normalizeStringList(member?.denied_tools),
    // The per-agent override object (any subset of model/temperature/thinking_effort),
    // or null when the agent has no override. Read shape-only here — the row derives
    // whether a field is overridden from `effective[field].source === 'override'`.
    overrides: normalizeOverrides(member?.overrides),
    // The provenance-aware resolved values, one entry per run field:
    // `{ value, source }`. A null value means "not configured" (model) or
    // "provider default" (temperature/thinking); a null source means no tier won.
    effective: normalizeEffective(member?.effective),
  }));
}

// Normalize the member's `overrides` object into a plain map of the known fields, or
// null when absent/empty. The value shapes are field-specific and passed through
// verbatim (model string, temperature number, thinking-effort string).
function normalizeOverrides(overrides) {
  if (!isPlainObject(overrides)) {
    return null;
  }
  const normalized = {};
  for (const field of TEAM_EFFECTIVE_FIELDS) {
    if (Object.hasOwn(overrides, field)) {
      normalized[field] = overrides[field];
    }
  }
  return Object.keys(normalized).length > 0 ? normalized : null;
}

// Normalize the member's `effective` map into `{ field: { value, source } }` for
// the known run fields. A missing field entry becomes `{ value: null, source:
// null }` so the row renders a stable "not configured / provider default" state
// rather than crashing on an absent key.
function normalizeEffective(effective) {
  const source = isPlainObject(effective) ? effective : {};
  const normalized = {};
  for (const field of TEAM_EFFECTIVE_FIELDS) {
    const entry = isPlainObject(source[field]) ? source[field] : {};
    normalized[field] = {
      value: entry.value ?? null,
      source: stringOrNull(entry.source),
    };
  }
  return normalized;
}

// Whether a team member currently has an override for the given field. Derived from
// `effective[field].source === 'override'`, the single truth for "overridden" that
// also drives the Clear-override control's visibility.
export function memberFieldIsOverridden(member, field) {
  return member?.effective?.[field]?.source === EFFECTIVE_SOURCE_OVERRIDE;
}

// Seed the per-field override draft (the values the override controls edit) for one
// team member. The model draft is the member's overridden model (or the
// effective/repo model as a starting suggestion), the temperature draft a text box
// seeded from the overridden/effective number, the thinking-effort draft the
// overridden/effective level. A blank draft means "nothing typed yet".
export function seedTeamOverrideDraft(member) {
  const overrides = isPlainObject(member?.overrides) ? member.overrides : {};
  const effective = member?.effective ?? {};

  const modelSeed = hasText(overrides.model)
    ? String(overrides.model)
    : effectiveTextValue(effective.model);
  const temperatureSeed = hasNumber(overrides.temperature)
    ? String(overrides.temperature)
    : effectiveTextValue(effective.temperature);
  const thinkingSeed =
    typeof overrides.thinking_effort === 'string'
      ? overrides.thinking_effort
      : effectiveTextValue(effective.thinking_effort);

  return {
    model: modelSeed,
    temperature: temperatureSeed,
    thinking_effort: thinkingSeed,
  };
}

// The temperature override value for the payload: a comma-tolerant number, or null
// when the box is empty/non-numeric (the Set button is disabled on null — an
// override must carry a value; clearing is a separate action).
export function normalizeOverrideTemperature(value) {
  return normalizeProjectTemperature(value);
}

function effectiveTextValue(entry) {
  const value = isPlainObject(entry) ? entry.value : null;
  return value === null || value === undefined ? '' : String(value);
}

function hasText(value) {
  return typeof value === 'string' && value.trim().length > 0;
}

function hasNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

// Normalize the scan report into a render-ready shape: the `clean` flag plus
// findings grouped by type in a stable order. An empty / clean report is the
// normal case (a bare or empty repo), NOT an error — `clean` is true and
// `groups` is empty, and callers must treat that as a healthy project.
export function normalizeScanReport(report) {
  const rawFindings = Array.isArray(report?.findings) ? report.findings : [];
  const findings = rawFindings.map((finding) => ({
    type: asText(finding?.type),
    detail: asText(finding?.detail),
    agent_id: asText(finding?.agent_id),
    source_path: optionalText(finding?.source_path),
  }));

  const groups = FINDING_TYPES.map((type) => ({
    type,
    findings: findings.filter((finding) => finding.type === type),
  })).filter((group) => group.findings.length > 0);

  // The server's `clean` flag is authoritative; fall back to "no findings" only
  // when it is absent so a malformed payload still renders sensibly.
  const clean =
    typeof report?.clean === 'boolean' ? report.clean : findings.length === 0;

  return {
    clean,
    findingCount: findings.length,
    findings,
    groups,
  };
}

// Trim + drop empties from a list-of-strings value (a non-array → []). The shared
// primitive behind auto_load and the whitelist list fields.
function normalizeStringList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => asText(item).trim())
    .filter((item) => item.length > 0);
}

function normalizeAutoLoad(value) {
  return normalizeStringList(value);
}

// Form temperature (a string, possibly comma-decimal) → number|null. Mirrors
// settingsView.js' normalizeAgentDefaultsTemperature: an empty/non-numeric box
// is "no value" (null), so the chain falls through.
function normalizeProjectTemperature(value) {
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return null;
  }
  const numberValue = Number(normalized.replace(',', '.'));
  return Number.isFinite(numberValue) ? numberValue : null;
}

// Form thinking effort (sentinel|''|level) → null|''|level for the payload.
// Mirrors settingsView.js' normalizeAgentDefaultsThinkingEffortForPayload: the
// sentinel and a missing value mean "no default" (null), '' means "provider
// default", and only a known level passes through (an unknown one → null).
function normalizeProjectThinkingEffortForPayload(value) {
  if (value === PROJECT_THINKING_EFFORT_NO_DEFAULT) {
    return null;
  }
  if (value === null || value === undefined) {
    return null;
  }
  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return '';
  }
  return PROJECT_THINKING_EFFORT_OPTIONS.includes(normalized)
    ? normalized
    : null;
}

function countOrZero(value) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : 0;
}

function numberOrNull(value) {
  return typeof value === 'number' ? value : null;
}

function stringOrNull(value) {
  return typeof value === 'string' ? value : null;
}

function sameStringList(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  return left.every((item, index) => item === right[index]);
}

// Order-insensitive equality for the membership-set whitelist fields.
function sameStringSet(left, right) {
  if (left.length !== right.length) {
    return false;
  }
  const rightSet = new Set(right);
  return left.every((item) => rightSet.has(item));
}

function optionalText(value) {
  const normalized = asText(value).trim();
  return normalized ? normalized : null;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function asText(value) {
  return value === null || value === undefined ? '' : String(value);
}
