import {
  dateKeyInApplicationZone,
  formatDateTimeInApplicationZone,
} from '../dateTimePrefs.svelte.js';
import { t } from '../i18n.js';

const EM_DASH = '\u2014';
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;

// ---------------------------------------------------------------------------
// Numbers

const numberFormats = new Map();

function numberFormat(locale, options) {
  const key = `${locale}|${JSON.stringify(options)}`;
  let format = numberFormats.get(key);
  if (!format) {
    format = new Intl.NumberFormat(locale, options);
    numberFormats.set(key, format);
  }
  return format;
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toFiniteNumber(value) {
  return finite(value) ?? 0;
}

/** A count in the locale's grouping; a dash for an unknown value. */
export function formatInteger(value, locale = 'en') {
  const number = finite(value);
  return number === null
    ? EM_DASH
    : numberFormat(locale, {}).format(Math.round(number));
}

/** A number with at most one decimal (an average); a dash when unknown. */
export function formatDecimal(value, locale = 'en') {
  const number = finite(value);
  return number === null
    ? EM_DASH
    : numberFormat(locale, { maximumFractionDigits: 1 }).format(number);
}

/** Tokens in compact form (950, 12K, 1.2M, 5.3B); a dash when unknown. */
export function formatTokens(value, locale = 'en') {
  const number = finite(value);
  if (number === null) return EM_DASH;
  if (Math.abs(number) < 1000) return formatInteger(number, locale);
  return numberFormat(locale, {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(number);
}

/** Tokens in full, for tooltips and the Limits tab. */
export function formatTokensExact(value, locale = 'en') {
  return formatInteger(value, locale);
}

/**
 * A USD amount with two decimals: a dash when unknown, `$0.00` for an exact
 * zero, and `<$0.01` for an amount that would round to zero.
 */
export function formatCost(value, locale = 'en') {
  const amount = finite(value);
  if (amount === null) return EM_DASH;
  const format = numberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (amount > 0 && amount < 0.005) return `<${format.format(0.01)}`;
  return format.format(amount);
}

/** A USD amount to the micro-dollar, for tooltips and single Model calls. */
export function formatCostExact(value, locale = 'en') {
  const amount = finite(value);
  if (amount === null) return EM_DASH;
  return numberFormat(locale, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  }).format(amount);
}

/** A ratio (0.25) as a percentage with one decimal; a dash when unknown. */
export function formatPercent(ratio, locale = 'en', fractionDigits = 1) {
  const value = finite(ratio);
  if (value === null) return EM_DASH;
  return numberFormat(locale, {
    style: 'percent',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value);
}

/** `value` as a share of `total`; a dash when the value is unknown or the
 *  total is not positive. */
export function formatShare(value, total, locale = 'en') {
  const part = finite(value);
  const whole = finite(total);
  if (part === null || whole === null || whole <= 0) return EM_DASH;
  return formatPercent(part / whole, locale);
}

/** A duration: `850 ms`, `12.3 s`, `4m 05s`, `2h 10m`; a dash when unknown.
 *  It rounds to a unit's precision before choosing the unit, so 59.96 s reads
 *  `1m 00s`, not `60.0 s`, and 3 h 59 m 30 s reads `4h 0m`, not `3h 60m`. */
export function formatDurationMs(milliseconds) {
  const value = finite(milliseconds);
  if (value === null) return EM_DASH;
  const ms = Math.round(Math.max(0, value));
  if (ms < 1000) return t('statistics.duration.milliseconds', { ms });
  const tenths = Math.round(ms / 100);
  if (tenths < 600) {
    return t('statistics.duration.seconds', {
      seconds: (tenths / 10).toFixed(1),
    });
  }
  const seconds = Math.round(ms / 1000);
  if (seconds < 3600) {
    return t('statistics.duration.minutesSeconds', {
      minutes: Math.floor(seconds / 60),
      seconds: String(seconds % 60).padStart(2, '0'),
    });
  }
  const minutes = Math.round(ms / MINUTE_MS);
  return t('statistics.duration.hoursMinutes', {
    hours: Math.floor(minutes / 60),
    minutes: minutes % 60,
  });
}

// Short duration for chart axes: 10s, 2m, 1h.
function formatDurationShort(milliseconds) {
  if (milliseconds >= HOUR_MS) {
    return t('statistics.duration.shortHours', {
      hours: Math.round(milliseconds / HOUR_MS),
    });
  }
  if (milliseconds >= MINUTE_MS) {
    return t('statistics.duration.shortMinutes', {
      minutes: Math.round(milliseconds / MINUTE_MS),
    });
  }
  return t('statistics.duration.shortSeconds', {
    seconds: Math.round(milliseconds / 1000),
  });
}

function parseIso(isoString) {
  if (typeof isoString !== 'string' || isoString.length === 0) return null;
  const date = new Date(isoString);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Date and time in the application time zone. */
export function formatDateTime(isoString, locale = 'en') {
  const date = parseIso(isoString);
  if (date === null) return EM_DASH;
  return formatDateTimeInApplicationZone(date, locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

/** A date for table cells, in the application time zone: day and time
 *  within the current year (`Sep 30, 9:30 AM`), the day with its year
 *  otherwise (`Sep 30, 2025`). */
export function formatShortDateTime(
  isoString,
  locale = 'en',
  now = new Date(),
) {
  const date = parseIso(isoString);
  if (date === null) return EM_DASH;
  const thisYear =
    dateKeyInApplicationZone(date).slice(0, 4) ===
    dateKeyInApplicationZone(now).slice(0, 4);
  return formatDateTimeInApplicationZone(
    date,
    locale,
    thisYear
      ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
      : { dateStyle: 'medium' },
  );
}

/** A local hour of the day (0-23) as `09:00`. */
export function formatHourLabel(hour) {
  const safeHour = Math.max(0, Math.min(23, Math.round(toFiniteNumber(hour))));
  return `${String(safeHour).padStart(2, '0')}:00`;
}

/** Cache hit rate: cache reads as a share of all input; null without input. */
export function cacheHitRate(record) {
  const input = toFiniteNumber(record?.input_tokens);
  if (input <= 0) return null;
  return toFiniteNumber(record?.cache_read_tokens) / input;
}

/** Input plus output tokens. */
export function totalTokens(record) {
  return (
    toFiniteNumber(record?.input_tokens) + toFiniteNumber(record?.output_tokens)
  );
}

/** USD per million input and output tokens; null when cost or tokens are
 *  unknown. */
export function costPerMillionTokens(record) {
  const cost = finite(record?.cost_usd);
  const tokens = totalTokens(record);
  if (cost === null || tokens <= 0) return null;
  return (cost / tokens) * 1_000_000;
}

/** A part of a whole as a ratio; null when the whole is not positive. */
export function shareOf(value, total) {
  const whole = finite(total);
  const part = finite(value);
  if (whole === null || whole <= 0 || part === null) return null;
  return part / whole;
}

export { numberFormat, finite, toFiniteNumber, formatDurationShort, parseIso };
