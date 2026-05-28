export const AGENT_FORM_MODE_CREATE = 'create';
export const AGENT_FORM_MODE_EDIT = 'edit';

export const DEFAULT_AGENT_TEMPERATURE = '';
export const DEFAULT_AGENT_ALLOWED_LIST = '*';
export const DEFAULT_AGENT_ALLOWED_TOOLS = Object.freeze([
  DEFAULT_AGENT_ALLOWED_LIST,
]);
export const DEFAULT_AGENT_ALLOWED_SKILLS = Object.freeze([
  DEFAULT_AGENT_ALLOWED_LIST,
]);

const EDITABLE_AGENT_FIELDS = Object.freeze([
  'name',
  'model',
  'fallback_model',
  'temperature',
  'thinking_effort',
  'workspace',
  'allowed_tools',
  'allowed_skills',
]);

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
    allowed_tools: normalizeList(
      agent.allowed_tools,
      DEFAULT_AGENT_ALLOWED_TOOLS,
    ),
    allowed_skills: normalizeArrayList(
      agent.allowed_skills,
      DEFAULT_AGENT_ALLOWED_SKILLS,
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

  if (mode === AGENT_FORM_MODE_EDIT && !normalized.workspace) {
    errors.workspace = 'required';
  }

  const temperature = normalizeTemperature(normalized.temperature);
  if (normalized.temperature && temperature === null) {
    errors.temperature = 'invalid_number';
  }

  const payloadOptions = { includeWorkspace: mode === AGENT_FORM_MODE_EDIT };
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
    allowed_tools: normalizeList(values.allowed_tools),
    allowed_skills: normalizeArrayList(values.allowed_skills),
  };
}

function normalizeList(items, fallback = []) {
  if (typeof items === 'string') {
    return textToList(items);
  }

  if (!Array.isArray(items)) {
    return [...fallback];
  }

  return items
    .map((item) => asText(item).trim())
    .filter((item) => item.length > 0);
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

  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function buildAgentPayload(normalized, temperature, options = {}) {
  const payload = {
    name: normalized.name,
    model: normalized.model,
    fallback_model: normalized.fallback_model,
    temperature,
    thinking_effort: normalized.thinking_effort || null,
    allowed_tools: normalized.allowed_tools,
    allowed_skills: normalized.allowed_skills,
  };

  if (options.includeWorkspace) {
    payload.workspace = normalized.workspace;
  }

  return payload;
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
