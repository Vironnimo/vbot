import { parseModelSelectionValue } from './modelSelection.js';
import { normalizeCompactionPolicy } from './compactionPolicy.js';
import {
  AGENT_TARGET_GROUP_PROJECT,
  buildAgentTargetOptions,
} from './agentTargetOptions.js';
import { parseAgentAddress } from './agentAddress.js';
import { normalizeToolAccess } from './toolAccess.js';
import { asText, isPlainObject } from './values.js';

export const AGENT_FORM_MODE_CREATE = 'create';
export const AGENT_FORM_MODE_EDIT = 'edit';

const DEFAULT_AGENT_ALLOWED_LIST = '*';
const DEFAULT_AGENT_ALLOWED_SKILLS = Object.freeze([
  DEFAULT_AGENT_ALLOWED_LIST,
]);
const DEFAULT_AGENT_MEMORY_PROMPT_MODE = 'agent_user';
// The sampling fields and their accepted ranges, matching the server's rules.
const SAMPLING_RANGES = Object.freeze({
  temperature: Object.freeze([0, 2]),
  top_p: Object.freeze([0, 1]),
});
export const AGENT_MEMORY_PROMPT_MODES = Object.freeze([
  'off',
  'agent',
  DEFAULT_AGENT_MEMORY_PROMPT_MODE,
]);

// The full thinking-effort ladder, empty first for the inherit option. Shared by
// the editor and the create modal so both offer the same ordered set (each then
// narrows it to the selected model's published reasoning ladder).
export const THINKING_EFFORT_OPTIONS = Object.freeze([
  '',
  'none',
  'minimal',
  'low',
  'medium',
  'high',
  'xhigh',
  'max',
]);

const EDITABLE_AGENT_FIELDS = Object.freeze([
  'name',
  'model',
  'fallback_models',
  'temperature',
  'top_p',
  'thinking_effort',
  'memory_prompt_mode',
  'workspace',
  'root_project_id',
  'tool_access',
  'allowed_skills',
  'excluded_skills',
  'tools',
  'custom_system_prompt_enabled',
  'compaction_policy',
  'librarian_enabled',
]);

const AGENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export function createAgentFormValues(agent = {}) {
  // The inheritable run fields (model/fallback_models/temperature/top_p/
  // thinking_effort) bind to the agent's RAW own values (`agent.config`), so an
  // empty/null raw value reads as the inherit state instead of the baked
  // top-level value. When no `config` block is present (create form, or an older
  // payload shape) the top-level values stand in, preserving prior behavior.
  const raw = isPlainObject(agent.config) ? agent.config : agent;
  return {
    id: asText(agent.id),
    name: asText(agent.name),
    model: asText(raw.model),
    fallback_models: normalizeArrayList(raw.fallback_models, []),
    workspace: asText(agent.workspace),
    root_project_id: hasValue(agent.root_project_id)
      ? String(agent.root_project_id)
      : null,
    temperature: hasValue(raw.temperature) ? String(raw.temperature) : '',
    top_p: hasValue(raw.top_p) ? String(raw.top_p) : '',
    thinking_effort: asText(raw.thinking_effort),
    memory_prompt_mode: normalizeMemoryPromptMode(agent.memory_prompt_mode),
    tool_access: normalizeToolAccess(agent.tool_access),
    allowed_skills: normalizeArrayList(
      agent.allowed_skills,
      DEFAULT_AGENT_ALLOWED_SKILLS,
    ),
    excluded_skills: normalizeArrayList(agent.excluded_skills, []),
    tools: normalizeAgentTools(agent.tools),
    custom_system_prompt_enabled: Boolean(agent.custom_system_prompt_enabled),
    compaction_policy: isPlainObject(raw.compaction_policy)
      ? normalizeCompactionPolicy(raw.compaction_policy)
      : null,
    // Librarian passes curate the Agent's own Skills unless switched off.
    librarian_enabled: agent.librarian_enabled !== false,
  };
}

// The form values once the saved Agent changed from `baseline` to `saved` (both
// `createAgentFormValues` results): a field the user has not edited, whose value
// still equals `baseline`, shows its `saved` value; an edited field keeps the
// user's value. Taken values are `saved`'s own objects.
export function rebaseAgentFormValues(values, baseline, saved) {
  const rebased = { ...values };
  for (const [field, value] of Object.entries(saved)) {
    if (JSON.stringify(values[field]) === JSON.stringify(baseline[field])) {
      rebased[field] = value;
    }
  }
  return rebased;
}

export function agentIdValidationError(value) {
  const errors = {};
  validateAgentId(asText(value).trim(), errors);
  return errors.id ?? '';
}

export function normalizeAgentForm(values, options = {}) {
  const mode = options.mode ?? AGENT_FORM_MODE_CREATE;
  const errors = {};
  const normalized = normalizeValues(values);

  if (mode === AGENT_FORM_MODE_CREATE) {
    validateAgentId(normalized.id, errors);
  }

  const sampling = {};
  for (const field of Object.keys(SAMPLING_RANGES)) {
    const { value, error } = parseSamplingValue(field, normalized[field]);
    sampling[field] = value;
    if (error) errors[field] = error;
  }

  const payloadOptions = {
    includeEmptyName: mode === AGENT_FORM_MODE_EDIT,
    includeWorkspace: mode === AGENT_FORM_MODE_EDIT,
    includeTools: mode === AGENT_FORM_MODE_EDIT,
    // A new Agent sends only the sampling values the user set, so an unset
    // field stays with the inherited value or the Provider default.
    includeEmptySampling: mode === AGENT_FORM_MODE_EDIT,
  };
  let payload = buildAgentPayload(normalized, sampling, payloadOptions);

  if (
    mode === AGENT_FORM_MODE_EDIT &&
    options.initialValues &&
    typeof options.initialValues === 'object'
  ) {
    const initialNormalized = normalizeValues(options.initialValues);
    const initialSampling = Object.fromEntries(
      Object.keys(SAMPLING_RANGES).map((field) => [
        field,
        parseSamplingValue(field, initialNormalized[field]).value,
      ]),
    );
    const initialPayload = buildAgentPayload(
      initialNormalized,
      initialSampling,
      payloadOptions,
    );
    payload = filterChangedFields(payload, initialPayload);
  }

  if (mode === AGENT_FORM_MODE_CREATE) {
    payload.id = normalized.id;
  } else if (hasValue(values?.id)) {
    payload.id = String(values.id).trim();
  }

  return {
    isValid: Object.keys(errors).length === 0,
    errors,
    payload,
    values: normalized,
  };
}

// The selected model's reasoning capability block, or null when the value is
// empty or the model is unknown/custom (the catalog has no entry). Shared by the
// editor and the create modal so both gate the thinking-effort options the same
// way. `models` is the `model.list` catalog array.
export function reasoningForModelValue(modelValue, models) {
  const { model } = parseModelSelectionValue(modelValue);
  if (!model) {
    return null;
  }
  const list = Array.isArray(models) ? models : [];
  const match = list.find((candidate) => candidate.id === model);
  return match?.capabilities?.reasoning ?? null;
}

// The selected Model's sampling recommendations from its `model.list` entry,
// each a number or null. They are only offered in the editor, never applied
// on their own.
export function samplingRecommendationsForModelValue(modelValue, models) {
  const { model } = parseModelSelectionValue(modelValue);
  const list = Array.isArray(models) ? models : [];
  const match = model ? list.find((candidate) => candidate.id === model) : null;
  return {
    temperature: finiteOrNull(match?.recommended_temperature),
    top_p: finiteOrNull(match?.recommended_top_p),
  };
}

// The thinking-effort options a model may show, gated by its reasoning ladder.
// No catalog info (unknown/custom model) or no published ladder keeps the full
// ladder — the adapter applies a provider-specific floor the UI cannot see, so
// it must not hide options that may be valid. A model with a published ladder
// shows only its possible efforts: the default (provider default, '') and "none"
// (reasoning off) always apply; the rest are exactly the model's levels, kept in
// canonical order so the dropdown reads consistently. A model whose reasoning
// is mandatory cannot turn it off, so it never offers "none".
export function effortOptionsForReasoning(reasoning) {
  const levels = Array.isArray(reasoning?.levels) ? reasoning.levels : [];
  const offered =
    reasoning?.mandatory === true
      ? THINKING_EFFORT_OPTIONS.filter((option) => option !== 'none')
      : THINKING_EFFORT_OPTIONS;
  if (levels.length === 0) {
    return offered;
  }
  const allowed = new Set(['', 'none', ...levels]);
  return offered.filter((option) => allowed.has(option));
}

function normalizeValues(values = {}) {
  return {
    id: asText(values.id).trim(),
    name: asText(values.name).trim(),
    model: asText(values.model).trim(),
    fallback_models: normalizeArrayList(values.fallback_models, []),
    workspace: asText(values.workspace).trim(),
    root_project_id: hasValue(values.root_project_id)
      ? String(values.root_project_id).trim() || null
      : null,
    temperature: asText(values.temperature).trim(),
    top_p: asText(values.top_p).trim(),
    thinking_effort: asText(values.thinking_effort).trim(),
    memory_prompt_mode: normalizeMemoryPromptMode(values.memory_prompt_mode),
    tool_access: normalizeToolAccess(values.tool_access),
    allowed_skills: normalizeArrayList(values.allowed_skills),
    excluded_skills: normalizeArrayList(values.excluded_skills, []),
    tools: normalizeAgentTools(values.tools),
    custom_system_prompt_enabled: Boolean(values.custom_system_prompt_enabled),
    compaction_policy: isPlainObject(values.compaction_policy)
      ? normalizeCompactionPolicy(values.compaction_policy)
      : null,
    librarian_enabled: values.librarian_enabled !== false,
  };
}

function normalizeArrayList(items, fallback = DEFAULT_AGENT_ALLOWED_SKILLS) {
  if (!Array.isArray(items)) {
    return [...fallback];
  }

  return items
    .map((item) => asText(item).trim())
    .filter((item) => item.length > 0);
}

// One sampling field's text as a number: empty is null (not set); text that is
// not a number or lies outside the field's range is an error.
function parseSamplingValue(field, text) {
  if (!text) {
    return { value: null, error: '' };
  }

  // Tolerate a comma decimal separator typed in comma-decimal locales.
  const numberValue = Number(asText(text).trim().replace(',', '.'));
  if (!Number.isFinite(numberValue)) {
    return { value: null, error: 'invalid_number' };
  }
  const [minimum, maximum] = SAMPLING_RANGES[field];
  if (numberValue < minimum || numberValue > maximum) {
    return { value: null, error: 'out_of_range' };
  }
  return { value: numberValue, error: '' };
}

function finiteOrNull(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function normalizeMemoryPromptMode(value) {
  const mode = asText(value).trim();
  return AGENT_MEMORY_PROMPT_MODES.includes(mode)
    ? mode
    : DEFAULT_AGENT_MEMORY_PROMPT_MODE;
}

function buildAgentPayload(normalized, sampling, options = {}) {
  const payload = {
    model: normalized.model,
    fallback_models: normalized.fallback_models,
    thinking_effort: normalized.thinking_effort || null,
    memory_prompt_mode: normalized.memory_prompt_mode,
    tool_access: normalized.tool_access,
    allowed_skills: normalized.allowed_skills,
    excluded_skills: normalized.excluded_skills,
    custom_system_prompt_enabled: normalized.custom_system_prompt_enabled,
    compaction_policy: normalized.compaction_policy,
    librarian_enabled: normalized.librarian_enabled,
  };

  for (const [field, value] of Object.entries(sampling)) {
    if (value !== null || options.includeEmptySampling) {
      payload[field] = value;
    }
  }

  if (normalized.name || options.includeEmptyName) {
    payload.name = normalized.name;
  }

  if (options.includeWorkspace) {
    payload.workspace = normalized.workspace;
    payload.root_project_id = normalized.root_project_id;
  }

  if (options.includeTools || Object.keys(normalized.tools).length > 0) {
    payload.tools = normalized.tools;
  }

  return payload;
}

export function subagentAllowedAgents(tools) {
  const allowed = tools?.subagent?.allowed_agents;
  return Array.isArray(allowed)
    ? normalizeArrayList(allowed, [])
    : [DEFAULT_AGENT_ALLOWED_LIST];
}

export function withSubagentAllowedAgents(tools, allowedAgents) {
  const next = normalizeAgentTools(tools);
  const normalizedAllowed = normalizeArrayList(allowedAgents, [
    DEFAULT_AGENT_ALLOWED_LIST,
  ]);
  const subagent = { ...(next.subagent ?? {}) };
  if (normalizedAllowed.includes(DEFAULT_AGENT_ALLOWED_LIST)) {
    delete subagent.allowed_agents;
  } else {
    subagent.allowed_agents = normalizedAllowed;
  }
  if (Object.keys(subagent).length > 0) {
    next.subagent = subagent;
  } else {
    delete next.subagent;
  }
  return next;
}

function normalizeAgentTools(tools) {
  if (!isPlainObject(tools)) {
    return {};
  }
  const normalizedTools = {};
  for (const [toolName, toolSettings] of Object.entries(tools)) {
    if (!toolName || !isPlainObject(toolSettings)) {
      continue;
    }
    normalizedTools[toolName] = { ...toolSettings };
  }
  const subagent = normalizedTools.subagent;
  if (isPlainObject(subagent) && 'allowed_agents' in subagent) {
    subagent.allowed_agents = normalizeArrayList(subagent.allowed_agents, []);
  }
  return normalizedTools;
}

// Build the global target catalog for an Identity Agent. Identity targets use a
// bare Agent id; Project targets use the canonical `agent@project` address. The
// catalog carries presentation metadata only — the persisted allow-list remains
// a list of exact address strings (or the `*` wildcard).
export function buildAgentTargetCatalog({
  identityAgents = [],
  projectTeams = [],
} = {}) {
  const teams = Array.isArray(projectTeams) ? projectTeams : [];
  const projectNames = new Map(
    teams.map((project) => [
      asText(project?.projectId),
      asText(project?.displayName) || asText(project?.projectId),
    ]),
  );
  const memberNames = new Map();
  for (const project of teams) {
    const projectId = asText(project?.projectId);
    for (const member of Array.isArray(project?.team) ? project.team : []) {
      const agentId = asText(member?.agent_id);
      if (projectId && agentId) {
        memberNames.set(
          `${projectId}:${agentId}`,
          asText(member?.display_name) || agentId,
        );
      }
    }
  }

  return buildAgentTargetOptions(identityAgents, teams).map((option) => {
    if (option.group !== AGENT_TARGET_GROUP_PROJECT) {
      return {
        name: option.value,
        displayName: option.label,
        kind: 'identity',
      };
    }
    const { agentId } = parseAgentAddress(option.value);
    return {
      name: option.value,
      displayName: memberNames.get(`${option.projectId}:${agentId}`) || agentId,
      kind: 'project',
      projectId: option.projectId,
      projectName: projectNames.get(option.projectId) || option.projectId,
    };
  });
}

function filterChangedFields(payload, baselinePayload) {
  const changedPayload = {};

  for (const fieldName of EDITABLE_AGENT_FIELDS) {
    if (valuesEqual(payload[fieldName], baselinePayload[fieldName])) {
      continue;
    }

    changedPayload[fieldName] = payload[fieldName];
  }

  return changedPayload;
}

function valuesEqual(left, right) {
  if (Array.isArray(left) || Array.isArray(right)) {
    return arrayValuesEqual(left, right);
  }

  if (isPlainObject(left) || isPlainObject(right)) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  return left === right;
}

function arrayValuesEqual(left, right) {
  if (!Array.isArray(left) || !Array.isArray(right)) {
    return false;
  }

  if (left.length !== right.length) {
    return false;
  }

  return left.every((item, index) => item === right[index]);
}

function validateAgentId(agentId, errors) {
  if (!agentId) {
    errors.id = 'required';
    return;
  }

  if (!AGENT_ID_PATTERN.test(agentId)) {
    errors.id = 'invalid_id';
  }
}

function hasValue(value) {
  return value !== null && value !== undefined;
}
