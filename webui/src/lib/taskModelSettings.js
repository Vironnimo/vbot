export const TASK_SPEECH_TO_TEXT = 'speech_to_text';
export const TASK_TEXT_TO_SPEECH = 'text_to_speech';
export const TASK_IMAGE_UNDERSTANDING = 'image_understanding';
export const TASK_IMAGE_GENERATION = 'image_generation';
export const TASK_VIDEO_GENERATION = 'video_generation';
export const TASK_MUSIC_GENERATION = 'music_generation';
export const TASK_TEXT_EMBEDDING = 'text_embedding';
export const TASK_LIVE_VOICE = 'live_voice';

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

const LIVE_VOICE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_LIVE_VOICE,
    titleKey: 'settings.specializedModels.liveVoice',
    titleFallback: 'Live voice',
    descriptionKey: 'settings.specializedModels.liveVoiceDescription',
    descriptionFallback:
      'Realtime voice model for spoken conversations with vBot. Delegating models also use a backend model to operate the app.',
  },
]);

const IMAGE_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_IMAGE_UNDERSTANDING,
    titleKey: 'settings.specializedModels.imageUnderstanding',
    titleFallback: 'Image understanding',
    descriptionKey: 'settings.specializedModels.imageUnderstandingDescription',
    descriptionFallback:
      'Used by analyze_image. Available by default for Agents without vision, or with vision when explicitly enabled in the Agent’s Tool settings.',
  },
  {
    taskType: TASK_IMAGE_GENERATION,
    titleKey: 'settings.specializedModels.imageGeneration',
    titleFallback: 'Image generation',
    descriptionKey: 'settings.specializedModels.imageGenerationDescription',
    descriptionFallback: 'Used for image generation requests.',
  },
]);

const TEXT_EMBEDDING_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_TEXT_EMBEDDING,
    titleKey: 'settings.specializedModels.embeddingModel',
    titleFallback: 'Embedding model',
    descriptionKey: 'settings.specializedModels.embeddingModelDescription',
    descriptionFallback:
      'Turns text into numeric vectors for meaning-based search. Required when Recall is set to Semantic.',
  },
]);

const GENERATED_MEDIA_TASK_ROWS = Object.freeze([
  {
    taskType: TASK_VIDEO_GENERATION,
    titleKey: 'settings.specializedModels.videoGeneration',
    titleFallback: 'Video generation',
    descriptionKey: 'settings.specializedModels.videoGenerationDescription',
    descriptionFallback: 'Used by the agent generate_video tool.',
  },
  {
    taskType: TASK_MUSIC_GENERATION,
    titleKey: 'settings.specializedModels.musicGeneration',
    titleFallback: 'Music generation',
    descriptionKey: 'settings.specializedModels.musicGenerationDescription',
    descriptionFallback: 'Used by the agent generate_music tool.',
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
    titleKey: 'settings.specializedModels.decision',
    titleFallback: 'Decision model',
    descriptionKey: 'settings.specializedModels.decisionDescription',
    descriptionFallback:
      'Structured judgments for the evaluate Tool and Jev experiments.',
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
      optionsBy: normalizeOptionsBy(field?.options_by),
    }))
    .filter((field) => field.name.length > 0);
}

// The choices a select shows for a binding's current options. A field with
// `optionsBy` shows only the choices listed for the referenced field's current
// value (stored, else its default); a value without an entry keeps them all.
export function visibleFieldOptions(field, fields, options) {
  const choices = field?.options ?? [];
  const optionsBy = field?.optionsBy;
  if (!optionsBy) {
    return choices;
  }
  const referenced = (fields ?? []).find(
    (candidate) => candidate.name === optionsBy.field,
  );
  const referencedValue = referenced
    ? effectiveOptionValue(referenced, options)
    : options?.[optionsBy.field];
  const key = textOrEmpty(referencedValue);
  if (!Object.hasOwn(optionsBy.values, key)) {
    return choices;
  }
  const allowed = new Set(optionsBy.values[key]);
  return choices.filter((choice) => allowed.has(choice.value));
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
