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
]);

// The list-valued whitelist fields, diffed by SET (order-insensitive) so a
// reorder alone never counts as a change. Tool/skill names are unordered membership
// sets; an empty list is a real value (e.g. every tool off).
const WHITELIST_LIST_FIELDS = Object.freeze([
  'allowed_tools',
  'skills_bundled_enabled',
  'skills_project_disabled',
]);

// The memory tool is runtime-derived from the agent's memory mode and never part
// of a project Tool Whitelist, so the editor hides it from the toggle catalog.
export const PROJECT_TOOL_WHITELIST_EXCLUDED = Object.freeze(['memory']);

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
    if (field === 'display_name' && next === '') {
      // display_name is required non-empty; an empty box is not a clear.
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

// Build the tool toggle rows for the editor: every catalog tool (minus the
// runtime-derived `memory` tool) with whether it is in the project's current Tool
// Whitelist. The catalog is the tool-catalog RPC's tool list, so new tools appear
// automatically. Rows are sorted by name for a stable display.
export function buildToolToggleList({ catalog = [], allowedTools = [] } = {}) {
  const excluded = new Set(PROJECT_TOOL_WHITELIST_EXCLUDED);
  const enabled = new Set(normalizeStringList(allowedTools));
  const names = (Array.isArray(catalog) ? catalog : [])
    .map((tool) => asText(typeof tool === 'string' ? tool : tool?.name).trim())
    .filter((name) => name.length > 0 && !excluded.has(name));
  const unique = Array.from(new Set(names)).sort();
  return unique.map((name) => ({ name, enabled: enabled.has(name) }));
}

// Build the two skill toggle sections for the editor from a project's skill pool
// and its stored whitelist rule. Project skills are on by default (off only when
// named in `skills_project_disabled`); bundled skills are off by default (on only
// when named in `skills_bundled_enabled`). A bundled skill shadowed by a project
// skill of the same name is dropped from the bundled section (project wins).
export function buildSkillToggleSections({
  projectSkills = [],
  bundledSkills = [],
  skillsBundledEnabled = [],
  skillsProjectDisabled = [],
} = {}) {
  const disabled = new Set(normalizeStringList(skillsProjectDisabled));
  const enabledBundled = new Set(normalizeStringList(skillsBundledEnabled));
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

// Normalize the scan response's skill pool into the editor's two name lists.
export function normalizeScanSkills(scan) {
  const skills = scan?.skills ?? {};
  return {
    project: normalizeStringList(skills.project),
    bundled: normalizeStringList(skills.bundled),
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
    auto_load: normalizeAutoLoad(project?.auto_load),
    allowed_tools: normalizeStringList(project?.allowed_tools),
    skills_bundled_enabled: normalizeStringList(
      project?.skills_bundled_enabled,
    ),
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

// Project the scan's team into a stable, display-ready list. The repo is the
// source of truth (no copy drift) — this only shapes what the view renders.
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
    // The per-agent model override (vBot-owned, top model-chain tier), or null.
    // The team row shows it with an `x` to clear; it is set only via /model.
    model_override: stringOrNull(member?.model_override),
  }));
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
