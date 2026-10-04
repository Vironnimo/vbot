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
  if (typeof text !== 'string' || text.trim().length === 0) {
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
// textarea. ``undefined``/``null`` and an empty object or array fall back to
// an empty string, so the control starts blank and shows its placeholder;
// non-JSON values are stringified verbatim.
export function stringifyJsonFieldValue(value) {
  if (value === undefined || value === null || isEmptyStructure(value)) {
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

// Presentation of each task row. `title` names the task: the accessible name
// of its target picker and, while task labels are shown, the row label.
// `label` replaces it as the visible row label where the section heading
// already names the task. `description` is an optional one-line hint shown
// under the label; `help` is the full explanation behind the row's "?".
const SPEECH_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_SPEECH_TO_TEXT,
    title: () => t('settings.specializedModels.speechToText'),
    help: () => t('settings.specializedModels.speechToTextHelp'),
  },
  {
    taskType: TASK_TEXT_TO_SPEECH,
    title: () => t('settings.specializedModels.textToSpeech'),
    help: () => t('settings.specializedModels.textToSpeechHelp'),
  },
]);

const LIVE_VOICE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_LIVE_VOICE,
    title: () => t('settings.specializedModels.liveVoice'),
    label: () => t('settings.specializedModels.liveVoiceModel'),
    description: () => t('settings.specializedModels.liveVoiceDescription'),
    help: () => t('settings.specializedModels.liveVoiceHelp'),
  },
]);

const IMAGE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_IMAGE_UNDERSTANDING,
    title: () => t('settings.specializedModels.imageUnderstanding'),
    description: () =>
      t('settings.specializedModels.imageUnderstandingDescription'),
    help: () => t('settings.specializedModels.imageUnderstandingHelp'),
  },
  {
    taskType: TASK_IMAGE_GENERATION,
    title: () => t('settings.specializedModels.imageGeneration'),
    help: () => t('settings.specializedModels.imageGenerationHelp'),
  },
]);

const TEXT_EMBEDDING_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_TEXT_EMBEDDING,
    title: () => t('settings.specializedModels.embeddingModel'),
    help: () => t('settings.specializedModels.embeddingModelHelp'),
  },
]);

const GENERATED_MEDIA_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_VIDEO_GENERATION,
    title: () => t('settings.specializedModels.videoGeneration'),
    help: () => t('settings.specializedModels.videoGenerationHelp'),
  },
  {
    taskType: TASK_MUSIC_GENERATION,
    title: () => t('settings.specializedModels.musicGeneration'),
    help: () => t('settings.specializedModels.musicGenerationHelp'),
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
    help: () => t('settings.specializedModels.decisionHelp'),
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

// `facts` are the task type's selection facts for one target (for
// `text_embedding`: local, multilingual, recommended_rank, note,
// input_price_per_million); other task types carry none. `metadata` describes
// the target itself (a local target: license, download_bytes, ...).
export function normalizeTargets(result) {
  return result.targets
    .map((target) => ({
      id: textOrEmpty(target?.id),
      label: textOrFallback(target?.label, target?.id),
      usable: target?.usable !== false,
      kind: textOrFallback(target?.kind, 'provider'),
      providerId: textOrEmpty(target?.provider_id),
      facts: plainCopy(target?.facts),
      metadata: plainCopy(target?.metadata),
    }))
    .filter((target) => target.id.length > 0);
}

function plainCopy(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? { ...value }
    : {};
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
      placeholder: textOrEmpty(field?.placeholder),
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

// The options of a previous target that a new target's fields accept as
// they are: the same name with a value the field can hold (an offered choice,
// a number in range, a switch, text, JSON). A select whose carried value the
// other carried options hide is dropped, so no hidden value is kept.
export function compatibleOptions(fields, options) {
  const byName = new Map((fields ?? []).map((field) => [field.name, field]));
  const kept = {};
  for (const [name, value] of Object.entries(options ?? {})) {
    const field = byName.get(name);
    if (field && value !== undefined && value !== null && value !== '') {
      if (optionValueFits(field, value)) {
        kept[name] = value;
      }
    }
  }
  for (const field of fields ?? []) {
    if (
      field.type === 'select' &&
      Object.hasOwn(kept, field.name) &&
      !visibleFieldOptions(field, fields, kept).some(
        (choice) => choice.value === kept[field.name],
      )
    ) {
      delete kept[field.name];
    }
  }
  return kept;
}

function optionValueFits(field, value) {
  switch (field.type) {
    case 'select':
      return field.options.some((choice) => choice.value === value);
    case 'number':
      return (
        typeof value === 'number' &&
        Number.isFinite(value) &&
        (field.min === null || value >= field.min) &&
        (field.max === null || value <= field.max)
      );
    case 'boolean':
      return typeof value === 'boolean';
    case JSON_OPTION_TYPE:
      return true;
    default:
      return typeof value === 'string';
  }
}

function isEmptyStructure(value) {
  return (
    typeof value === 'object' &&
    (Array.isArray(value) ? value : Object.keys(value)).length === 0
  );
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
