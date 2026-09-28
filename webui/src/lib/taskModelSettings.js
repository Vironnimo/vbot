import { t } from './i18n.js';

const TASK_SPEECH_TO_TEXT = 'speech_to_text';
const TASK_TEXT_TO_SPEECH = 'text_to_speech';
const TASK_IMAGE_UNDERSTANDING = 'image_understanding';
const TASK_IMAGE_GENERATION = 'image_generation';
const TASK_VIDEO_GENERATION = 'video_generation';
const TASK_MUSIC_GENERATION = 'music_generation';
const TASK_TEXT_EMBEDDING = 'text_embedding';
const TASK_LIVE_VOICE = 'live_voice';

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
    title: () => t('settings.specializedModels.speechToText'),
    description: () => t('settings.specializedModels.speechToTextDescription'),
  },
  {
    taskType: TASK_TEXT_TO_SPEECH,
    title: () => t('settings.specializedModels.textToSpeech'),
    description: () => t('settings.specializedModels.textToSpeechDescription'),
  },
]);

const LIVE_VOICE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_LIVE_VOICE,
    title: () => t('settings.specializedModels.liveVoice'),
    description: () => t('settings.specializedModels.liveVoiceDescription'),
  },
]);

const IMAGE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_IMAGE_UNDERSTANDING,
    title: () => t('settings.specializedModels.imageUnderstanding'),
    description: () =>
      t('settings.specializedModels.imageUnderstandingDescription'),
  },
  {
    taskType: TASK_IMAGE_GENERATION,
    title: () => t('settings.specializedModels.imageGeneration'),
    description: () =>
      t('settings.specializedModels.imageGenerationDescription'),
  },
]);

const TEXT_EMBEDDING_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_TEXT_EMBEDDING,
    title: () => t('settings.specializedModels.embeddingModel'),
    description: () =>
      t('settings.specializedModels.embeddingModelDescription'),
  },
]);

const GENERATED_MEDIA_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_VIDEO_GENERATION,
    title: () => t('settings.specializedModels.videoGeneration'),
    description: () =>
      t('settings.specializedModels.videoGenerationDescription'),
  },
  {
    taskType: TASK_MUSIC_GENERATION,
    title: () => t('settings.specializedModels.musicGeneration'),
    description: () =>
      t('settings.specializedModels.musicGenerationDescription'),
  },
]);

export const TASK_MODEL_ROWS = Object.freeze([
  ...SPEECH_TASK_ROWS,
  ...LIVE_VOICE_TASK_ROWS,
  ...IMAGE_TASK_ROWS,
  ...GENERATED_MEDIA_TASK_ROWS,
  ...TEXT_EMBEDDING_TASK_ROWS,
  {
    taskType: 'decision',
    title: () => t('settings.specializedModels.decision'),
    description: () => t('settings.specializedModels.decisionDescription'),
  },
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
  return result.targets
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
      optionsBy: normalizeOptionsBy(field?.options_by),
    }))
    .filter((field) => field.name.length > 0);
}

// The choices a select shows for a binding's current options. A field with
// `optionsBy` shows only the choices listed for the referenced field's current
// value (stored, else its default); a value without an entry keeps them all.
export function visibleFieldOptions(field, fields, options) {
  const choices = field?.options ?? [];
  const allowed = narrowedChoiceValues(field, fields, options);
  if (allowed === null) {
    return choices;
  }
  const shown = new Set(allowed);
  return choices.filter((choice) => shown.has(choice.value));
}

// Whether a field is hidden for a binding's current options: its `optionsBy`
// entry for the referenced value is an empty list, so that value makes the
// field irrelevant. The stored value stays in the binding and is ignored.
export function isOptionFieldHidden(field, fields, options) {
  const allowed = narrowedChoiceValues(field, fields, options);
  return allowed !== null && allowed.length === 0;
}

// After the option `changedName` changes, move every select whose choices
// depend on it off a value that is no longer shown: to its default when shown,
// else to '' when shown, else to the first shown choice. Dependents of a moved
// field follow the same rule. Returns `options` itself when nothing moves.
export function reconcileDependentOptions(fields, options, changedName) {
  let next = options ?? {};
  const pending = [changedName];
  const moved = new Set();
  while (pending.length > 0) {
    const name = pending.shift();
    for (const field of fields ?? []) {
      if (field.optionsBy?.field !== name || moved.has(field.name)) {
        continue;
      }
      const shown = visibleFieldOptions(field, fields, next).map(
        (choice) => choice.value,
      );
      if (
        shown.length === 0 ||
        shown.includes(effectiveOptionValue(field, next))
      ) {
        continue;
      }
      const fallback =
        [field.default, ''].find((value) => shown.includes(value)) ?? shown[0];
      next = { ...next, [field.name]: fallback };
      moved.add(field.name);
      pending.push(field.name);
    }
  }
  return next;
}

export function createTaskModelUpdatePayload(bindings, previous) {
  const payload = {};
  const baseline = previous ? createTaskModelUpdatePayload(previous) : null;
  for (const row of TASK_MODEL_ROWS) {
    const binding = normalizeBinding(bindings?.[row.taskType]);
    payload[row.taskType] = {
      target: binding.target,
      options: normalizeOptionsForPayload(binding.options),
    };
    if (
      baseline &&
      JSON.stringify(payload[row.taskType]) ===
        JSON.stringify(baseline[row.taskType])
    ) {
      delete payload[row.taskType];
    }
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

// An empty value is a real choice (for example "Provider default"); only
// choices without any value are dropped.
function normalizeFieldOptions(options) {
  if (!Array.isArray(options)) {
    return [];
  }
  return options
    .filter((option) => option?.value !== undefined && option?.value !== null)
    .map((option) => ({
      value: textOrEmpty(option.value),
      label: textOrFallback(option.label, option.value),
    }));
}

function normalizeOptionsBy(optionsBy) {
  if (!optionsBy || typeof optionsBy !== 'object') {
    return null;
  }
  const field = textOrEmpty(optionsBy.field);
  const source = optionsBy.values;
  if (
    !field ||
    !source ||
    typeof source !== 'object' ||
    Array.isArray(source)
  ) {
    return null;
  }
  const values = {};
  for (const [key, allowed] of Object.entries(source)) {
    if (Array.isArray(allowed)) {
      values[key] = allowed.map((value) => textOrEmpty(value));
    }
  }
  return { field, values };
}

// The choice values `optionsBy` lists for the referenced field's current
// value, or null when the field is not narrowed for that value.
function narrowedChoiceValues(field, fields, options) {
  const optionsBy = field?.optionsBy;
  if (!optionsBy) {
    return null;
  }
  const referenced = (fields ?? []).find(
    (candidate) => candidate.name === optionsBy.field,
  );
  const referencedValue = referenced
    ? effectiveOptionValue(referenced, options)
    : options?.[optionsBy.field];
  const key = textOrEmpty(referencedValue);
  return Object.hasOwn(optionsBy.values, key) ? optionsBy.values[key] : null;
}

// The value a field shows: the stored draft value, else its schema default.
function effectiveOptionValue(field, options) {
  const value = options?.[field.name];
  return value === undefined || value === null ? field.default : value;
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
