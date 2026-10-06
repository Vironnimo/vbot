import { asText, isPlainObject } from '../values.js';
import { normalizeCompactionPolicy } from '../compactionPolicy.js';
import { normalizeToolAccess } from '../toolAccess.js';

export const emptyScanSkills = () => ({ project: [], bundled: [], global: [] });

export function createProjectAddForm() {
  return {
    cwd: '',
    display_name: '',
  };
}

export function createProjectEditForm(project = null) {
  return {
    display_name: project?.display_name ?? '',
    default_agent: project?.default_agent ?? '',
    default_model: project?.default_model ?? '',
    sources: (project?.sources ?? []).map((source) => ({ ...source })),
    model_mappings: { ...(project?.model_mappings ?? {}) },
    default_temperature:
      typeof project?.default_temperature === 'number'
        ? String(project.default_temperature)
        : '',
    default_top_p:
      typeof project?.default_top_p === 'number'
        ? String(project.default_top_p)
        : '',
    default_thinking_effort:
      project?.default_thinking_effort === null ||
      project?.default_thinking_effort === undefined
        ? PROJECT_THINKING_EFFORT_NO_DEFAULT
        : project.default_thinking_effort,
    auto_load: [...(project?.auto_load ?? [])],
    allowed_tools: [...(project?.allowed_tools ?? [])],
    skills_bundled_enabled: [...(project?.skills_bundled_enabled ?? [])],
    skills_global_enabled: [...(project?.skills_global_enabled ?? [])],
    skills_project_disabled: [...(project?.skills_project_disabled ?? [])],
  };
}

export function createProjectsState({ selectedProjectId = '' } = {}) {
  return {
    projects: [],
    // The first Project list has been applied.
    projectsLoaded: false,
    loadingProjects: false,
    listError: '',
    statusMessage: '',
    availableModels: [],
    availableConnections: [],
    modelDropdownOpenCount: 0,
    pendingModelCatalogs: null,
    lastModelsRefreshToken: null,
    lastProjectsRefreshToken: null,
    globalAgentDefaults: {},
    globalCompactionPolicy: null,
    isAddOpen: false,
    addForm: createProjectAddForm(),
    addingProject: false,
    addError: '',
    addDetect: null,
    selectedProjectId,
    editForm: createProjectEditForm(),
    editSaving: false,
    editError: '',
    autoLoadDraft: '',
    activeTeam: [],
    activeReport: null,
    activeSources: [],
    shadowedTeam: [],
    activeScanSkills: emptyScanSkills(),
    scanLoading: false,
    scanRefreshRequested: false,
    removingProjectId: '',
    removeConfirmProject: null,
    copyRootedAgentIdentityFiles: false,
    // Whether the confirmed removal skips the Archive.
    removePermanently: false,
    expandedMembers: {},
    overrideDrafts: {},
    overrideBusyKey: '',
    toolCatalog: [],
    defaultProjectTools: [],
    rePointProject: null,
    rePointCwd: '',
    rePointing: false,
    rePointError: '',
    showAllModels: false,
    showAllOverrideModels: {},
  };
}

// Pure view helpers for the Projects tab. Business and normalization logic
// lives here so the Svelte component stays a thin display/input/orchestration
// layer (see webui.md → Conventions).
//
// The shapes mirror the verified backend contract (server/rpc/project_methods):
//   project: { project_id, display_name, cwd, cwd_exists, default_agent,
//              default_model, auto_load[], created_at, updated_at }
//   scan:    { team: [member…], report: { clean, findings: [finding…] } }
//   finding: { type, detail, agent_id, source_path }

// The scan report's `finding.type` discriminants (server scan_report.py).
const FINDING_TYPE_SLUG_COLLISION = 'slug_collision';

const FINDING_TYPE_UNSLUGIFIABLE_NAME = 'unslugifiable_name';

const FINDING_TYPE_BAD_MODEL = 'bad_model';

const FINDING_TYPE_ORPHAN = 'orphan';

const FINDING_TYPE_UNAVAILABLE_TOOL = 'unavailable_tool';

// Stable display order for grouped findings, so the report always lists the
// same finding kinds in the same order regardless of server ordering.
const FINDING_TYPES = Object.freeze([
  FINDING_TYPE_SLUG_COLLISION,
  FINDING_TYPE_UNSLUGIFIABLE_NAME,
  FINDING_TYPE_BAD_MODEL,
  FINDING_TYPE_ORPHAN,
  FINDING_TYPE_UNAVAILABLE_TOOL,
  'invalid_source',
  'skill_collision',
]);

// The mutable fields a manage form can change through project.set. cwd is
// handled by the dedicated re-point path, and the sampling defaults
// (default_temperature / default_top_p) and default_thinking_effort have their
// own typed diff (number/null and null/''/level), so they are not part of this
// generic string-trim diff.
const MANAGE_FIELDS = Object.freeze([
  'display_name',
  'default_agent',
  'default_model',
]);

// Fields in the generic diff that are required non-empty on the backend: an
// empty form value is "no change", never a clear-to-null. Display name is
// intentionally clearable and then falls back to the stable Project id.
const NON_CLEARABLE_MANAGE_FIELDS = Object.freeze(new Set());

// The Project's sampling defaults, each a number or null.
const PROJECT_SAMPLING_FIELDS = Object.freeze([
  'default_temperature',
  'default_top_p',
]);

// The list-valued whitelist fields, diffed by SET (order-insensitive) so a
// reorder alone never counts as a change. Tool/skill names are unordered membership
// sets; an empty list is a real value (e.g. every tool off).
const WHITELIST_LIST_FIELDS = Object.freeze([
  'allowed_tools',
  'skills_bundled_enabled',
  'skills_global_enabled',
  'skills_project_disabled',
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
// thin api wrapper enforces it too); the optional display name is only included
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

  if (Array.isArray(formValues?.sources)) payload.sources = formValues.sources;
  if (isPlainObject(formValues?.model_mappings))
    payload.model_mappings = formValues.model_mappings;

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
// trimmed string. A cleared display_name is likewise sent as null and the
// Project domain falls back to project_id.
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

  // Sampling: form string → number|null; send only when it differs from the
  // stored value. null clears the project default (fall through the chain), a
  // number sets it (0 is a real value, the sampling floor).
  for (const field of PROJECT_SAMPLING_FIELDS) {
    const next = normalizeProjectNumber(formValues?.[field]);
    if (next !== numberOrNull(project?.[field])) {
      changes[field] = next;
    }
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

  for (const field of ['sources', 'model_mappings']) {
    const fallback = field === 'sources' ? [] : {};
    const next = formValues?.[field] ?? fallback;
    if (JSON.stringify(next) !== JSON.stringify(project?.[field] ?? fallback))
      changes[field] = next;
  }
  return changes;
}

// Build the tool toggle rows for the editor: every server-marked Project-configurable
// catalog tool with whether it is in the project's current Tool Whitelist. The
// tool-catalog RPC owns that policy, so new tools and policy changes appear without a
// frontend name list. Rows are sorted by
// name for a stable display. Each row carries the tool's readiness fields
// (`ready`/`readiness_hint`/`extension`) so a not-ready tool renders the shared
// "currently unavailable" notice (its toggle stays functional — the whitelist is
// independent of readiness).
export function buildToolToggleList({ catalog = [], allowedTools = [] } = {}) {
  const enabled = new Set(normalizeStringList(allowedTools));
  const byName = new Map();
  for (const tool of catalog) {
    const name = asText(tool.name).trim();
    if (
      name.length === 0 ||
      tool.project_configurable === false ||
      byName.has(name)
    ) {
      continue;
    }
    byName.set(name, {
      name,
      family: tool.family ?? null,
      family_label: tool.family_label ?? null,
      description: tool.description,
      enabled: enabled.has(name),
      ready: tool.ready !== false,
      readiness_hint: tool.readiness_hint ?? null,
      extension: tool.extension ?? null,
    });
  }

  // Keep persisted entries that disappeared from the live catalog visible and
  // removable. This is common when an Extension is temporarily disabled: the
  // backend deliberately preserves the permission, reports it as unavailable,
  // and only rejects *new* unknown grants. A stale row therefore stays on and
  // gets the shared not-ready treatment until the tool returns or the user turns
  // it off. Project-excluded names are included only through this recovery path;
  // they can never be newly selected from the catalog.
  for (const name of enabled) {
    if (byName.has(name)) {
      continue;
    }
    byName.set(name, {
      name,
      family: null,
      family_label: null,
      description: '',
      enabled: true,
      ready: false,
      readiness_hint: null,
      extension: null,
      registered: false,
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
// Normalize one skill-pool entry to `{ name, description }`. The scan sends
// `{name, description}` objects (so the whitelist chips can show a description on
// hover); a bare string is tolerated too and gets an empty description. Nameless
// entries drop out.
function normalizeSkillPoolEntry(entry) {
  if (entry !== null && typeof entry === 'object') {
    const name = asText(entry.name).trim();
    return name ? { name, description: asText(entry.description) } : null;
  }
  const name = asText(entry).trim();
  return name ? { name, description: '' } : null;
}

function normalizeSkillPool(list) {
  return (Array.isArray(list) ? list : [])
    .map(normalizeSkillPoolEntry)
    .filter(Boolean);
}

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
  const projectEntries = normalizeSkillPool(projectSkills);
  const projectSet = new Set(projectEntries.map((skill) => skill.name));
  return {
    project: projectEntries.map((skill) => ({
      name: skill.name,
      description: skill.description,
      enabled: !disabled.has(skill.name),
    })),
    bundled: normalizeSkillPool(bundledSkills)
      .filter((skill) => !projectSet.has(skill.name))
      .map((skill) => ({
        name: skill.name,
        description: skill.description,
        enabled: enabledBundled.has(skill.name),
      })),
    global: normalizeSkillPool(globalSkills)
      .filter((skill) => !projectSet.has(skill.name))
      .map((skill) => ({
        name: skill.name,
        description: skill.description,
        enabled: enabledGlobal.has(skill.name),
      })),
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

// Normalize the scan response's skill pool into the editor's `{name, description}`
// lists (each group carries descriptions for the whitelist chips' hover cards).
export function normalizeScanSkills(scan) {
  const skills = scan?.skills ?? {};
  return {
    project: normalizeSkillPool(skills.project),
    bundled: normalizeSkillPool(skills.bundled),
    global: normalizeSkillPool(skills.global),
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

export function normalizeDetectResult(result) {
  return {
    cwd_exists: result?.cwd_exists === true,
    sources: Array.isArray(result?.sources) ? result.sources : [],
  };
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
    default_top_p: numberOrNull(project?.default_top_p),
    default_thinking_effort: stringOrNull(project?.default_thinking_effort),
    sources: Array.isArray(project?.sources)
      ? project.sources.map(({ id, enabled, agent_paths }) => ({
          id,
          enabled: enabled !== false,
          ...(Array.isArray(agent_paths)
            ? { agent_paths: [...agent_paths] }
            : {}),
        }))
      : [],
    model_mappings: isPlainObject(project?.model_mappings)
      ? { ...project.model_mappings }
      : {},
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

// The per-agent overridable / effective run fields, in display order. Each is
// resolved through the config-agent chain (override → agent file → project default →
// global default) and reported by the scan as `effective[field] = {value, source}`.
const TEAM_EFFECTIVE_FIELDS = Object.freeze([
  'model',
  'thinking_effort',
  'temperature',
  'top_p',
]);

// The winning-source discriminant the scan reports on `effective[field].source`
// when a per-agent override wins.
const EFFECTIVE_SOURCE_OVERRIDE = 'override';

// Project the scan's team into a stable, display-ready list. The repo is the
// source of truth (no copy drift) — this only shapes what the view renders. Each
// member carries its raw repo-declared values (for reference), the per-agent
// `overrides` object (or null), and the `effective` map of `{value, source}` per
// run field so the row can show the resolved value with provenance.
//
// NOTE: `agent_id` and `display_name` are consumed by Chat's Agent picker
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
    top_p: typeof member?.top_p === 'number' ? member.top_p : null,
    thinking_effort: stringOrNull(member?.thinking_effort),
    source: asText(member?.source),
    status: asText(member?.status) || 'ready',
    available: member?.available !== false,
    translations: Array.isArray(member?.translations)
      ? member.translations
      : [],
    unavailable_reason: asText(member?.unavailable_reason),
    source_path: asText(member?.source_path),
    denied_tools: normalizeStringList(member?.denied_tools),
    tools:
      member?.tools && typeof member.tools === 'object' ? member.tools : {},
    // The per-agent override object (any subset of the run fields above),
    // or null when the agent has no override. Read shape-only here — the row derives
    // whether a field is overridden from `effective[field].source === 'override'`.
    overrides: normalizeOverrides(member?.overrides),
    // The provenance-aware resolved values, one entry per run field:
    // `{ value, source }`. A null value means "not configured" (model) or
    // "provider default" (sampling/thinking); a null source means no tier won.
    effective: normalizeEffective(member?.effective),
  }));
}

// Summarize one Project Agent's repository-owned Sub-Agent targets against the
// current Team. Project targets are always local bare ids; there is deliberately
// no vBot override tier for this policy.
export function projectAgentTargetSummary(member, team = []) {
  const configured = member?.tools?.subagent?.allowed_agents;
  if (!Array.isArray(configured)) {
    return { mode: 'unavailable', agents: [] };
  }
  const callerAgentId = asText(member?.agent_id).trim();
  const allowed = normalizeStringList(configured).filter(
    (agentId) => agentId !== callerAgentId,
  );
  const teamIds = (Array.isArray(team) ? team : [])
    .map((candidate) => asText(candidate?.agent_id).trim())
    .filter((agentId) => agentId && agentId !== callerAgentId);
  if (allowed.length === 0) {
    return { mode: 'self', agents: [] };
  }
  const allowedSet = new Set(allowed);
  if (
    teamIds.length > 0 &&
    allowed.length === teamIds.length &&
    teamIds.every((agentId) => allowedSet.has(agentId))
  ) {
    return { mode: 'all', agents: allowed };
  }
  return { mode: 'limited', agents: allowed };
}

// Normalize the member's `overrides` object into a plain map of the known fields, or
// null when absent/empty. The value shapes are field-specific and passed through
// verbatim (model string, sampling number, thinking-effort string).
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
  if (isPlainObject(overrides.compaction_policy)) {
    normalized.compaction_policy = normalizeCompactionPolicy(
      overrides.compaction_policy,
    );
  }
  if (isPlainObject(overrides.tool_access)) {
    normalized.tool_access = normalizeToolAccess(overrides.tool_access);
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
  const toolAccessEntry = isPlainObject(source.tool_access)
    ? source.tool_access
    : {};
  normalized.tool_access = {
    value: normalizeToolAccess(toolAccessEntry.value),
    source: stringOrNull(toolAccessEntry.source),
  };
  return normalized;
}

// Whether a team member currently has an override for the given field. Derived from
// `effective[field].source === 'override'`, the single truth for "overridden" that
// also drives the Clear-override control's visibility.
export function memberFieldIsOverridden(member, field) {
  if (field === 'compaction_policy') {
    return isPlainObject(member?.overrides?.compaction_policy);
  }
  if (field === 'tool_access') {
    return isPlainObject(member?.overrides?.tool_access);
  }
  return member?.effective?.[field]?.source === EFFECTIVE_SOURCE_OVERRIDE;
}

// Seed the per-field override draft (the values the override controls edit) for one
// team member. The model draft is the member's overridden model (or the
// effective/repo model as a starting suggestion), the thinking-effort draft the
// overridden/effective level. Each sampling draft (temperature, top_p) holds only
// the override itself: an empty box means "no override", so the inherited value,
// or the Provider default, stays in effect and is never copied into an override.
export function seedTeamOverrideDraft(member) {
  const overrides = isPlainObject(member?.overrides) ? member.overrides : {};
  const effective = member?.effective ?? {};

  const modelSeed = hasText(overrides.model)
    ? String(overrides.model)
    : effectiveTextValue(effective.model);
  const samplingSeed = (field) =>
    hasNumber(overrides[field]) ? String(overrides[field]) : '';
  const thinkingSeed =
    typeof overrides.thinking_effort === 'string'
      ? overrides.thinking_effort
      : effectiveTextValue(effective.thinking_effort);

  return {
    model: modelSeed,
    temperature: samplingSeed('temperature'),
    top_p: samplingSeed('top_p'),
    thinking_effort: thinkingSeed,
    compaction_policy: isPlainObject(overrides.compaction_policy)
      ? normalizeCompactionPolicy(overrides.compaction_policy)
      : null,
    tool_access: isPlainObject(overrides.tool_access)
      ? normalizeToolAccess(overrides.tool_access)
      : normalizeToolAccess(effective.tool_access?.value),
  };
}

// A sampling override value (temperature, top_p) for the payload: a
// comma-tolerant number, or null when the box is empty/non-numeric. An emptied
// box clears the override (see the controller's savePendingOverrides).
export function normalizeOverrideNumber(value) {
  return normalizeProjectNumber(value);
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

// Form sampling value (a string, possibly comma-decimal) → number|null. Mirrors
// settingsView.js' normalizeAgentDefaultsNumber: an empty/non-numeric box is
// "no value" (null), so the chain falls through.
function normalizeProjectNumber(value) {
  const normalized = String(value).trim();
  if (normalized.length === 0) {
    return null;
  }
  const numberValue = Number(normalized.replace(',', '.'));
  return Number.isFinite(numberValue) ? numberValue : null;
}

// Form thinking effort (sentinel|''|level) → null|''|level for the payload.
// Mirrors settingsView.js' normalizeAgentDefaultsThinkingEffortForPayload: the
// sentinel means "no default" (null), '' means "provider default", and only a
// known level passes through (anything else → null).
function normalizeProjectThinkingEffortForPayload(value) {
  if (value === PROJECT_THINKING_EFFORT_NO_DEFAULT) {
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
