import { SvelteDate } from 'svelte/reactivity';

export const dateTimePrefs = $state({
  timeZone: 'UTC',
});

export function setApplicationTimeZone(value) {
  dateTimePrefs.timeZone = isSupportedTimeZone(value) ? value : 'UTC';
}

// Creating an Intl.DateTimeFormat costs far more than formatting with one, and
// the Chat Timeline formats every row's times on each render, so formatters
// are reused per locale, options and zone.
const MAX_CACHED_FORMATTERS = 64;
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- a cache, not state: formatting runs inside derived values, which must not write reactive state
const formatters = new Map();

function dateTimeFormat(locale, options) {
  const key = JSON.stringify([locale ?? null, options]);
  let formatter = formatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, options);
    if (formatters.size >= MAX_CACHED_FORMATTERS) {
      formatters.delete(formatters.keys().next().value);
    }
    formatters.set(key, formatter);
  }
  return formatter;
}

export function formatDateTimeInApplicationZone(value, locale, options = {}) {
  const date = value instanceof Date ? value : new SvelteDate(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return dateTimeFormat(locale, {
    ...options,
    timeZone: dateTimePrefs.timeZone,
  }).format(date);
}

// Date keys of timestamp strings, per zone: the Chat Timeline derives one for
// every row on each update, and parsing and formatting cost far more than a
// lookup. A key is a pure function of zone and timestamp.
const MAX_CACHED_DATE_KEYS = 4096;
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- a cache, not state: formatting runs inside derived values, which must not write reactive state
const dateKeys = new Map();

export function dateKeyInApplicationZone(value = new SvelteDate()) {
  if (typeof value !== 'string') {
    return formatDateKey(value instanceof Date ? value : new SvelteDate(value));
  }
  const cacheKey = `${dateTimePrefs.timeZone} ${value}`;
  let dateKey = dateKeys.get(cacheKey);
  if (dateKey === undefined) {
    dateKey = formatDateKey(new SvelteDate(value));
    if (dateKeys.size >= MAX_CACHED_DATE_KEYS) {
      dateKeys.delete(dateKeys.keys().next().value);
    }
    dateKeys.set(cacheKey, dateKey);
  }
  return dateKey;
}

function formatDateKey(date) {
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const parts = dateTimeFormat('en-CA', {
    timeZone: dateTimePrefs.timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date);
  const values = Object.fromEntries(
    parts.map((part) => [part.type, part.value]),
  );
  return `${values.year}-${values.month}-${values.day}`;
}

// Explicit-zone primitives for wall-clock editors and report windows. Callers
// own parsing, invalid-zone fallback, rounding and serialization.
export function dateTimePartsInZone(value, timeZone) {
  const date = value instanceof Date ? value : new SvelteDate(value);
  const parts = dateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

// Milliseconds the zone's wall clock runs ahead of UTC. Whole-second
// projection preserves the instant's subsecond precision at the caller.
export function timeZoneOffsetMilliseconds(instant, timeZone) {
  const parts = dateTimePartsInZone(instant, timeZone);
  const wall = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour),
    Number(parts.minute),
    Number(parts.second),
  );
  return wall - Math.floor(Number(instant) / 1000) * 1000;
}

function isSupportedTimeZone(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return false;
  }
  try {
    new Intl.DateTimeFormat('en', { timeZone: value }).format();
    return true;
  } catch {
    return false;
  }
}
