import { t } from './i18n.js';

// The `resource` of the change an Extension publishes whenever its pending
// inputs gain or lose an entry (`PENDING_INPUTS_RESOURCE` in
// `core/extensions/operations.py`); any owner may publish it.
export const PENDING_INPUTS_RESOURCE = 'pending_inputs';

// The text control for each string `format` a form may ask for.
const TEXT_TYPES = {
  email: 'email',
  uri: 'url',
  date: 'date',
  'date-time': 'datetime-local',
};
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const WALL_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

// A request's form as fields to render: one per property of its
// `requestedSchema`, with the control (`kind`) that fits the property's type
// and the limits the answer must keep. Presentation only: the Extension
// validates the complete response.
export function inputFields(request) {
  const schema = request?.payload?.requestedSchema ?? {};
  const required = new Set(
    Array.isArray(schema.required) ? schema.required : [],
  );
  return Object.entries(schema.properties ?? {}).map(([key, property]) =>
    inputField(key, property ?? {}, required.has(key)),
  );
}

function inputField(key, property, required) {
  const field = {
    key,
    label: property.title || key,
    description: property.description ?? '',
    required,
    default: property.default,
    kind: 'json',
  };
  const choices = choiceOptions(property);
  if (choices) return { ...field, kind: 'select', options: choices };
  if (property.type === 'array') {
    const items = choiceOptions(property.items ?? {});
    if (items)
      return {
        ...field,
        kind: 'multiselect',
        options: items,
        minItems: property.minItems,
        maxItems: property.maxItems,
      };
  }
  if (property.type === 'boolean') return { ...field, kind: 'boolean' };
  if (['number', 'integer'].includes(property.type))
    return {
      ...field,
      kind: 'number',
      integer: property.type === 'integer',
      minimum: property.minimum,
      maximum: property.maximum,
    };
  if (property.type === 'string')
    return {
      ...field,
      kind: 'text',
      format: property.format ?? '',
      type: TEXT_TYPES[property.format] ?? 'text',
      minLength: property.minLength,
      maxLength: property.maxLength,
    };
  return field;
}

// The choices of a single-select property: titled `oneOf`/`anyOf` constants,
// or an `enum` with the legacy `enumNames` as labels.
function choiceOptions(schema) {
  const titled = schema.oneOf ?? schema.anyOf;
  if (Array.isArray(titled) && titled.every((choice) => 'const' in choice))
    return titled.map((choice) => ({
      value: String(choice.const),
      label: choice.title || String(choice.const),
      const: choice.const,
    }));
  if (Array.isArray(schema.enum))
    return schema.enum.map((value, index) => ({
      value: String(value),
      label: schema.enumNames?.[index] || String(value),
      const: value,
    }));
  return null;
}

// The starting answers: each field's schema default, in the form its control
// edits. A checkbox always has a value, so a boolean starts as false.
export function initialInputDrafts(request, { timeZone = 'UTC' } = {}) {
  const drafts = {};
  for (const field of inputFields(request)) {
    const value = field.default;
    if (field.kind === 'boolean') drafts[field.key] = value === true;
    else if (value === undefined) continue;
    else if (field.kind === 'select') {
      const option = field.options.find((item) => item.const === value);
      if (option) drafts[field.key] = option.value;
    } else if (field.kind === 'multiselect')
      drafts[field.key] = field.options
        .filter((item) => Array.isArray(value) && value.includes(item.const))
        .map((item) => item.value);
    else if (field.kind === 'json') drafts[field.key] = JSON.stringify(value);
    else if (field.format === 'date-time')
      drafts[field.key] = wallTime(value, timeZone);
    else drafts[field.key] = String(value);
  }
  return drafts;
}

// The problem of each answer the form cannot accept, by field key; an empty
// object means every answer fits its field.
export function validateInput(request, drafts) {
  const errors = {};
  for (const field of inputFields(request)) {
    const problem = fieldProblem(field, drafts[field.key]);
    if (problem) errors[field.key] = problem;
  }
  return errors;
}

function fieldProblem(field, value) {
  if (field.kind === 'boolean') return '';
  if (field.kind === 'multiselect') {
    const count = Array.isArray(value) ? value.length : 0;
    if (!count && !field.required) return '';
    if (field.minItems != null && count < field.minItems)
      return t('extensions.inputChoicesMin', { count: field.minItems });
    if (!count) return t('extensions.inputRequired');
    if (field.maxItems != null && count > field.maxItems)
      return t('extensions.inputChoicesMax', { count: field.maxItems });
    return '';
  }
  if (value === undefined || value === '')
    return field.required ? t('extensions.inputRequired') : '';
  if (field.kind === 'number') return numberProblem(field, value);
  if (field.kind === 'json') {
    try {
      JSON.parse(value);
      return '';
    } catch {
      return t('extensions.inputJson');
    }
  }
  if (field.kind === 'text') return textProblem(field, value);
  return '';
}

function numberProblem(field, value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return t('extensions.inputNumber');
  if (field.integer && !Number.isInteger(number))
    return t('extensions.inputInteger');
  if (field.minimum != null && number < field.minimum)
    return t('extensions.inputMinimum', { minimum: field.minimum });
  if (field.maximum != null && number > field.maximum)
    return t('extensions.inputMaximum', { maximum: field.maximum });
  return '';
}

function textProblem(field, value) {
  if (field.minLength != null && value.length < field.minLength)
    return t('extensions.inputMinLength', { count: field.minLength });
  if (field.maxLength != null && value.length > field.maxLength)
    return t('extensions.inputMaxLength', { count: field.maxLength });
  if (field.format === 'email' && !EMAIL.test(value))
    return t('extensions.inputEmail');
  if (field.format === 'uri' && !isAbsoluteUri(value))
    return t('extensions.inputUri');
  if (field.format === 'date' && !validDate(DATE.exec(value)))
    return t('extensions.inputDate');
  if (field.format === 'date-time' && !validDate(WALL_TIME.exec(value)))
    return t('extensions.inputDateTime');
  return '';
}

function isAbsoluteUri(value) {
  try {
    return Boolean(new URL(value).protocol);
  } catch {
    return false;
  }
}

function validDate(match) {
  if (!match) return false;
  const [year, month, day] = match.slice(1, 4).map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

// The response to `request`: `accept` carries the typed answers, a date and
// time as RFC 3339 with the offset of `timeZone` (the app's time zone, in
// which the user entered it). `decline` refuses; `cancel` ends the request
// without an answer.
export function inputResponse(
  request,
  drafts,
  action = 'accept',
  { timeZone = 'UTC' } = {},
) {
  if (request.kind === 'oauth')
    return { redirect_url: drafts.redirect_url ?? '', action };
  if (action !== 'accept') return { action };
  if (request.payload?.mode === 'url') return { action };
  const content = {};
  for (const field of inputFields(request)) {
    const value = drafts[field.key];
    if (field.kind === 'boolean') content[field.key] = value === true;
    else if (field.kind === 'multiselect') {
      const chosen = field.options.filter((item) =>
        (value ?? []).includes(item.value),
      );
      if (chosen.length || field.required)
        content[field.key] = chosen.map((item) => item.const);
    } else if (value === undefined || value === '') continue;
    else if (field.kind === 'select')
      content[field.key] =
        field.options.find((item) => item.value === value)?.const ?? value;
    else if (field.kind === 'number') content[field.key] = Number(value);
    else if (field.kind === 'json') content[field.key] = JSON.parse(value);
    else if (field.format === 'date-time')
      content[field.key] = zonedDateTime(value, timeZone);
    else content[field.key] = value;
  }
  return { action, content };
}

// The wall time `text` (`YYYY-MM-DDTHH:MM[:SS]`) in `timeZone` as RFC 3339
// with that zone's offset at that moment, or '' when it is no wall time.
export function zonedDateTime(text, timeZone = 'UTC') {
  const match = WALL_TIME.exec(text ?? '');
  if (!match) return '';
  const [year, month, day, hour, minute, second = 0] = match
    .slice(1)
    .map((part) => (part === undefined ? undefined : Number(part)));
  const wall = Date.UTC(year, month - 1, day, hour, minute, second);
  // The offset at the wall time read as UTC, then once more at the instant
  // that offset names, which settles a daylight saving change in between.
  let offset = zoneOffsetMinutes(wall, timeZone);
  offset = zoneOffsetMinutes(wall - offset * 60_000, timeZone);
  const sign = offset < 0 ? '-' : '+';
  const absolute = Math.abs(offset);
  const pad = (value) => String(value).padStart(2, '0');
  return (
    `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:` +
    `${pad(second)}${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`
  );
}

function zoneParts(instant, timeZone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(new Date(instant));
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

function zoneOffsetMinutes(instant, timeZone) {
  const part = zoneParts(instant, timeZone);
  const asUtc = Date.UTC(
    Number(part.year),
    Number(part.month) - 1,
    Number(part.day),
    Number(part.hour),
    Number(part.minute),
    Number(part.second),
  );
  return Math.round((asUtc - Math.floor(instant / 1000) * 1000) / 60_000);
}

// An RFC 3339 instant as the wall time a `datetime-local` control shows in
// `timeZone`, or '' when it is no instant.
function wallTime(value, timeZone) {
  const instant = Date.parse(value);
  if (Number.isNaN(instant)) return '';
  const part = zoneParts(instant, timeZone);
  return `${part.year}-${part.month}-${part.day}T${part.hour}:${part.minute}`;
}
