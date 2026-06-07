export const TASK_SPEECH_TO_TEXT = 'speech_to_text';
export const TASK_TEXT_TO_SPEECH = 'text_to_speech';
export const TASK_IMAGE_GENERATION = 'image_generation';

export const JSON_OPTION_TYPE = 'json';

// Result of parsing a JSON field's text input. The Settings UI keeps the
// last valid value in the binding and shows the error message inline;
// when ``error`` is non-empty, ``value`` is ``undefined`` and the binding
// must not be updated with the typed text.
export function parseJsonFieldValue(text) {
  if (typeof text !== 'string' || text.length === 0) {
    return { value: undefined, error: '' };
  }
  try {
    return { value: JSON.parse(text), error: '' };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return { value: undefined, error: message };
  }
}

// Render a stored JSON value (object/array/primitive) for display in a
// textarea. ``undefined``/``null`` fall back to an empty string so the
// control starts blank; non-JSON values are stringified verbatim.
export function stringifyJsonFieldValue(value) {
  if (value === undefined || value === null) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const SPEECH_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_SPEECH_TO_TEXT,
    titleKey: 'settings.specializedModels.speechToText',
    titleFallback: 'Speech to text',
    descriptionKey: 'settings.specializedModels.speechToTextDescription',
    descriptionFallback: 'Used by the chat microphone transcription flow.',
  },
  {
    taskType: TASK_TEXT_TO_SPEECH,
    titleKey: 'settings.specializedModels.textToSpeech',
    titleFallback: 'Text to speech',
    descriptionKey: 'settings.specializedModels.textToSpeechDescription',
    descriptionFallback: 'Used by the agent text_to_speech tool.',
  },
]);

const IMAGE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_IMAGE_GENERATION,
    titleKey: 'settings.specializedModels.imageGeneration',
    titleFallback: 'Image generation',
    descriptionKey: 'settings.specializedModels.imageGenerationDescription',
    descriptionFallback: 'Used for image generation requests.',
  },
]);

export const TASK_MODEL_ROWS = Object.freeze([
  ...SPEECH_TASK_ROWS,
  ...IMAGE_TASK_ROWS,
]);

export function normalizeTaskModelSettings(settings) {
  const source = settings?.model_tasks ?? settings ?? {};
  if (!source || typeof source !== 'object' || Array.isArray(source)) {
    return {};
  }

  const normalized = {};
  for (const row of TASK_MODEL_ROWS) {
    const binding = source[row.taskType];
    normalized[row.taskType] = normalizeBinding(binding);
  }
  return normalized;
}

export function normalizeTargets(result) {
  const targets = Array.isArray(result?.targets) ? result.targets : result;
  if (!Array.isArray(targets)) {
    return [];
  }

  return targets
    .map((target) => ({
      id: textOrEmpty(target?.id),
      label: textOrFallback(target?.label, target?.id),
      usable: target?.usable !== false,
      kind: textOrFallback(target?.kind, 'provider'),
    }))
    .filter((target) => target.id.length > 0);
}

export function normalizeOptionSchema(result) {
  const schema = result?.schema ?? result ?? {};
  const fields = Array.isArray(schema?.fields) ? schema.fields : [];
  return fields
    .map((field) => ({
      name: textOrEmpty(field?.name),
      type: textOrFallback(field?.type, 'text'),
      label: textOrFallback(field?.label, field?.name),
      default: field?.default ?? '',
      required: field?.required === true,
      description: textOrEmpty(field?.description),
      min: Number.isFinite(field?.min) ? field.min : null,
      max: Number.isFinite(field?.max) ? field.max : null,
      step: Number.isFinite(field?.step) ? field.step : null,
      options: normalizeFieldOptions(field?.options),
    }))
    .filter((field) => field.name.length > 0);
}

export function applyOptionDefaults(binding, fields) {
  const options = { ...(binding?.options ?? {}) };
  for (const field of fields ?? []) {
    if (
      options[field.name] === undefined &&
      field.default !== undefined &&
      field.default !== null
    ) {
      options[field.name] = field.default;
    }
  }
  return { ...normalizeBinding(binding), options };
}

export function createTaskModelUpdatePayload(bindings) {
  const payload = {};
  for (const row of TASK_MODEL_ROWS) {
    const binding = normalizeBinding(bindings?.[row.taskType]);
    payload[row.taskType] = {
      target: binding.target,
      options: normalizeOptionsForPayload(binding.options),
    };
  }
  return payload;
}

export function taskModelBindingsMatch(left, right) {
  return (
    JSON.stringify(createTaskModelUpdatePayload(left)) ===
    JSON.stringify(createTaskModelUpdatePayload(right))
  );
}

function normalizeBinding(binding) {
  const source = binding && typeof binding === 'object' ? binding : {};
  return {
    target: textOrEmpty(source.target),
    options:
      source.options && typeof source.options === 'object'
        ? { ...source.options }
        : {},
  };
}

function normalizeOptionsForPayload(options) {
  const normalized = {};
  const source = options && typeof options === 'object' ? options : {};
  for (const [key, value] of Object.entries(source)) {
    if (value === undefined) {
      continue;
    }
    normalized[key] = value;
  }
  return normalized;
}

function normalizeFieldOptions(options) {
  if (!Array.isArray(options)) {
    return [];
  }
  return options
    .map((option) => ({
      value: textOrEmpty(option?.value),
      label: textOrFallback(option?.label, option?.value),
    }))
    .filter((option) => option.value.length > 0);
}

function textOrEmpty(value) {
  if (value === null || value === undefined) {
    return '';
  }
  return String(value).trim();
}

function textOrFallback(value, fallback) {
  const normalized = textOrEmpty(value);
  return normalized.length > 0 ? normalized : textOrEmpty(fallback);
}
