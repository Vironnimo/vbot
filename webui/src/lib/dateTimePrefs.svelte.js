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

export function dateKeyInApplicationZone(value = new SvelteDate()) {
  const date = value instanceof Date ? value : new SvelteDate(value);
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
