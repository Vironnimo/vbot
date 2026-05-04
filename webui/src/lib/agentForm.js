export const AGENT_FORM_MODE_CREATE = 'create';
export const AGENT_FORM_MODE_EDIT = 'edit';

export const DEFAULT_AGENT_TEMPERATURE = '0.1';
export const DEFAULT_AGENT_ALLOWED_LIST = '*';

const AGENT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const EMPTY_TEXT = '';

export function createAgentFormValues(agent = {}) {
  return {
    id: asText(agent.id),
    name: asText(agent.name),
    model: asText(agent.model),
    fallback_model: asText(agent.fallback_model),
    workspace: asText(agent.workspace),
    temperature: hasValue(agent.temperature)
      ? String(agent.temperature)
      : DEFAULT_AGENT_TEMPERATURE,
    thinking_effort: asText(agent.thinking_effort),
    allowed_tools: listToText(agent.allowed_tools, DEFAULT_AGENT_ALLOWED_LIST),
    allowed_skills: listToText(
      agent.allowed_skills,
      DEFAULT_AGENT_ALLOWED_LIST,
    ),
  };
}

export function normalizeAgentForm(values, options = {}) {
  const mode = options.mode ?? AGENT_FORM_MODE_CREATE;
  const errors = {};
  const normalized = normalizeValues(values);

  if (mode === AGENT_FORM_MODE_CREATE) {
    validateAgentId(normalized.id, errors);
  }

  if (!normalized.name) {
    errors.name = 'required';
  }

  const temperature = Number(normalized.temperature);
  if (!normalized.temperature || !Number.isFinite(temperature)) {
    errors.temperature = 'invalid_number';
  }

  const payload = {
    name: normalized.name,
    model: normalized.model,
    fallback_model: normalized.fallback_model,
    temperature,
    thinking_effort: normalized.thinking_effort,
    allowed_tools: textToList(normalized.allowed_tools),
    allowed_skills: textToList(normalized.allowed_skills),
  };

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

export function listToText(items, fallback = EMPTY_TEXT) {
  if (!Array.isArray(items) || items.length === 0) {
    return fallback;
  }

  return items.join('\n');
}

function normalizeValues(values = {}) {
  return {
    id: asText(values.id).trim(),
    name: asText(values.name).trim(),
    model: asText(values.model).trim(),
    fallback_model: asText(values.fallback_model).trim(),
    workspace: asText(values.workspace).trim(),
    temperature: asText(values.temperature).trim(),
    thinking_effort: asText(values.thinking_effort).trim(),
    allowed_tools: asText(values.allowed_tools),
    allowed_skills: asText(values.allowed_skills),
  };
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
