// Time text for tooltips and details: a moment in the application time zone
// together with its distance from now ("Sep 29, 2026, 3:04 PM · 12 minutes
// ago"), so a reader sees both when something happened and how long ago.

import { activeLocaleTag } from '$lib/i18n.js';
import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const RELATIVE_UNITS = [
  ['year', 365 * DAY],
  ['month', 30 * DAY],
  ['week', 7 * DAY],
  ['day', DAY],
  ['hour', HOUR],
  ['minute', MINUTE],
];
// Below this distance a moment reads as "now".
const NOW_SECONDS = 45;

const relativeFormatters = new Map();

function relativeFormatter(locale) {
  let formatter = relativeFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
    relativeFormatters.set(locale, formatter);
  }
  return formatter;
}

function toMs(value) {
  if (value instanceof Date) {
    return value.getTime();
  }
  if (typeof value === 'number') {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    return Date.parse(value);
  }
  return Number.NaN;
}

/** Date and time in the application time zone; '' for a missing value. */
export function formatAbsoluteTime(value, { seconds = false } = {}) {
  const ms = toMs(value);
  if (!Number.isFinite(ms)) {
    return '';
  }
  return formatDateTimeInApplicationZone(new Date(ms), activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: seconds ? 'medium' : 'short',
  });
}

/** Distance from `nowMs`: "now", "12 minutes ago", "in 3 hours". */
export function formatRelativeTime(value, nowMs = Date.now()) {
  const ms = toMs(value);
  if (!Number.isFinite(ms) || !Number.isFinite(nowMs)) {
    return '';
  }
  const diffSeconds = (ms - nowMs) / 1000;
  const distance = Math.abs(diffSeconds);
  const formatter = relativeFormatter(activeLocaleTag());
  if (distance < NOW_SECONDS) {
    return formatter.format(0, 'second');
  }
  for (const [unit, size] of RELATIVE_UNITS) {
    if (distance >= size) {
      return formatter.format(Math.round(diffSeconds / size), unit);
    }
  }
  // 45-59 seconds away rounds to one minute.
  return formatter.format(Math.sign(diffSeconds), 'minute');
}

/** The absolute moment followed by its distance from now. */
export function formatMoment(value, { nowMs = Date.now(), seconds } = {}) {
  const absolute = formatAbsoluteTime(value, { seconds });
  if (!absolute) {
    return '';
  }
  return `${absolute} · ${formatRelativeTime(value, nowMs)}`;
}
