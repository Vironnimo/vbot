import { SvelteDate } from 'svelte/reactivity';

export const dateTimePrefs = $state({
  timeZone: 'UTC',
});

export function setApplicationTimeZone(value) {
  dateTimePrefs.timeZone = isSupportedTimeZone(value) ? value : 'UTC';
}

export function formatDateTimeInApplicationZone(value, locale, options = {}) {
  const date = value instanceof Date ? value : new SvelteDate(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return new Intl.DateTimeFormat(locale, {
    ...options,
    timeZone: dateTimePrefs.timeZone,
  }).format(date);
}

export function dateKeyInApplicationZone(value = new SvelteDate()) {
  const date = value instanceof Date ? value : new SvelteDate(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const parts = new Intl.DateTimeFormat('en-CA', {
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
