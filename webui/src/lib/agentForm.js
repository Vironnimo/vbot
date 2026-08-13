import { parseModelSelectionValue } from './modelSelection.js';
import { normalizeCompactionPolicy } from './compactionPolicy.js';
import {
  AGENT_TARGET_GROUP_PROJECT,
  buildAgentTargetOptions,
} from './agentTargetOptions.js';
import { parseAgentAddress } from './agentAddress.js';
import { normalizeToolAccess } from './toolAccess.js';

export const AGENT_FORM_MODE_CREATE = 'create';
export const AGENT_FORM_MODE_EDIT = 'edit';

const DEFAULT_AGENT_TEMPERATURE = '';
const DEFAULT_AGENT_ALLOWED_LIST = '*';
const DEFAULT_AGENT_ALLOWED_SKILLS = Object.freeze([
  DEFAULT_AGENT_ALLOWED_LIST,
]);
const DEFAULT_AGENT_MEMORY_PROMPT_MODE = 'agent_user';
export const MEMORY_TOOL_NAME = 'memory';
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
  'fallback_model',
  'temperature',
  'thinking_effort',
  'memory_prompt_mode',
  'workspace',
  'root_project_id',
  'tool_access',
  'allowed_skills',
  'tools',
  'custom_system_prompt_enabled',
  'compaction_policy',
]);

const AGENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const EMPTY_TEXT = '';

export function createAgentFormValues(agent = {}) {
  // The four inheritable run fields (model/fallback_model/temperature/
  // thinking_effort) bind to the agent's RAW own values (`agent.config`), so an
  // empty/null raw value reads as the inherit state instead of the baked
  // top-level value. When no `config` block is present (create form, or an older
  // payload shape) the top-level values stand in, preserving prior behavior.
  const raw = isPlainObject(agent.config) ? agent.config : agent;
  return {
    id: asText(agent.id),
    name: asText(agent.name),
    model: asText(raw.model),
    fallback_model: asText(raw.fallback_model),
    workspace: asText(agent.workspace),
    root_project_id: hasValue(agent.root_project_id)
      ? String(agent.root_project_id)
      : null,
    temperature: hasValue(raw.temperature)
      ? String(raw.temperature)
      : DEFAULT_AGENT_TEMPERATURE,
    thinking_effort: asText(raw.thinking_effort),
    memory_prompt_mode: normalizeMemoryPromptMode(agent.memory_prompt_mode),
    tool_access: normalizeToolAccess(agent.tool_access),
    allowed_skills: normalizeArrayList(
      agent.allowed_skills,
      DEFAULT_AGENT_ALLOWED_SKILLS,
    ),
    tools: normalizeAgentTools(agent.tools),
    custom_system_prompt_enabled: Boolean(agent.custom_system_prompt_enabled),
    compaction_policy: isPlainObject(raw.compaction_policy)
      ? normalizeCompactionPolicy(raw.compaction_policy)
      : null,
  };
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

  const temperature = normalizeTemperature(normalized.temperature);
  if (normalized.temperature && temperature === null) {
    errors.temperature = 'invalid_number';
  }

  const payloadOptions = {
    includeEmptyName: mode === AGENT_FORM_MODE_EDIT,
    includeWorkspace: mode === AGENT_FORM_MODE_EDIT,
    includeTools: mode === AGENT_FORM_MODE_EDIT,
  };
  let payload = buildAgentPayload(normalized, temperature, payloadOptions);

  if (
    mode === AGENT_FORM_MODE_EDIT &&
    options.initialValues &&
    typeof options.initialValues === 'object'
  ) {
    const initialNormalized = normalizeValues(options.initialValues);
    const initialPayload = buildAgentPayload(
      initialNormalized,
      normalizeTemperature(initialNormalized.temperature),
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

export function textToList(text) {
  if (!hasValue(text)) {
    return [];
  }

  return String(text)
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
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

// The thinking-effort options a model may show, gated by its reasoning ladder.
// No catalog info (unknown/custom model) or no published ladder keeps the full
// ladder — the adapter applies a provider-specific floor the UI cannot see, so
// it must not hide options that may be valid. A model with a published ladder
// shows only its possible efforts: the default (provider default, '') and "none"
// (reasoning off) always apply; the rest are exactly the model's levels, kept in
// canonical order so the dropdown reads consistently.
export function effortOptionsForReasoning(reasoning) {
  const levels = Array.isArray(reasoning?.levels) ? reasoning.levels : [];
  if (levels.length === 0) {
    return THINKING_EFFORT_OPTIONS;
  }
  const allowed = new Set(['', 'none', ...levels]);
  return THINKING_EFFORT_OPTIONS.filter((option) => allowed.has(option));
}

function normalizeValues(values = {}) {
  return {
    id: asText(values.id).trim(),
    name: asText(values.name).trim(),
    model: asText(values.model).trim(),
    fallback_model: asText(values.fallback_model).trim(),
    workspace: asText(values.workspace).trim(),
    root_project_id: hasValue(values.root_project_id)
      ? String(values.root_project_id).trim() || null
      : null,
    temperature: asText(values.temperature).trim(),
    thinking_effort: asText(values.thinking_effort).trim(),
    memory_prompt_mode: normalizeMemoryPromptMode(values.memory_prompt_mode),
    tool_access: normalizeToolAccess(values.tool_access),
    allowed_skills: normalizeArrayList(values.allowed_skills),
    tools: normalizeAgentTools(values.tools),
    custom_system_prompt_enabled: Boolean(values.custom_system_prompt_enabled),
    compaction_policy: isPlainObject(values.compaction_policy)
      ? normalizeCompactionPolicy(values.compaction_policy)
      : null,
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

function normalizeTemperature(value) {
  if (!value) {
    return null;
  }

  // Tolerate a comma decimal separator typed in comma-decimal locales.
  const numberValue = Number(asText(value).trim().replace(',', '.'));
  return Number.isFinite(numberValue) ? numberValue : null;
}

function normalizeMemoryPromptMode(value) {
  const mode = asText(value).trim();
  return AGENT_MEMORY_PROMPT_MODES.includes(mode)
    ? mode
    : DEFAULT_AGENT_MEMORY_PROMPT_MODE;
}

function buildAgentPayload(normalized, temperature, options = {}) {
  const payload = {
    model: normalized.model,
    fallback_model: normalized.fallback_model,
    temperature,
    thinking_effort: normalized.thinking_effort || null,
    memory_prompt_mode: normalized.memory_prompt_mode,
    tool_access: normalized.tool_access,
    allowed_skills: normalized.allowed_skills,
    custom_system_prompt_enabled: normalized.custom_system_prompt_enabled,
    compaction_policy: normalized.compaction_policy,
  };

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

function asText(value) {
  return hasValue(value) ? String(value) : EMPTY_TEXT;
}

function hasValue(value) {
  return value !== null && value !== undefined;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
