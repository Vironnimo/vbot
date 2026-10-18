import { formatDateTimeInApplicationZone } from '../dateTimePrefs.svelte.js';
import { t } from '../i18n.js';
import {
  finite,
  toFiniteNumber,
  totalTokens,
  numberFormat,
  formatTokens,
  parseIso,
  formatDurationShort,
  formatHourLabel,
} from './formatting.js';

const EM_DASH = '\u2014';
const DAY_MS = 86_400_000;

export const RUN_ORIGINS = Object.freeze([
  'user',
  'subagent',
  'extension',
  'automation',
  'channel',
  'reflection',
  'system',
  'background',
]);

// ---------------------------------------------------------------------------
// Charts

export const TREND_GRANULARITIES = Object.freeze(['day', 'week', 'month']);

const CHART_SCALE_STEPS = Object.freeze([1, 1.5, 2, 2.5, 5, 10]);
const COUNT_SCALE_STEPS = Object.freeze([1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]);

/** The smallest "nice" axis maximum (1, 1.5, 2, 2.5, 5 x 10^n) at or above
 *  `value`; 0 for an empty chart. A count axis (`integer`) keeps whole
 *  ticks: its maximum and midpoint are integers, so it is at least 2. */
export function niceScaleMax(value, { integer = false } = {}) {
  const number = finite(value);
  if (number === null || number <= 0) return 0;
  const magnitude = 10 ** Math.floor(Math.log10(number));
  if (integer) {
    for (const scale of [magnitude, magnitude * 10]) {
      for (const candidate of COUNT_SCALE_STEPS) {
        const maximum = Math.round(candidate * scale * 1e6) / 1e6;
        if (maximum >= number - 1e-9 && maximum >= 2 && maximum % 2 === 0) {
          return maximum;
        }
      }
    }
  }
  const step = CHART_SCALE_STEPS.find(
    (candidate) => candidate * magnitude >= number - 1e-9,
  );
  return step * magnitude;
}

function parseDateKey(dateKey) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateKey ?? ''));
  if (!match) return null;
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function dateKeyOf(utcMidnight) {
  return new Date(utcMidnight).toISOString().slice(0, 10);
}

// The first day of the period a local date key belongs to: the Monday of its
// ISO week, or the first of its month.
function periodKey(dateKey, granularity) {
  const day = parseDateKey(dateKey);
  if (day === null || granularity === 'day') return dateKey;
  const date = new Date(day);
  if (granularity === 'month') {
    return dateKeyOf(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
  }
  const weekday = (date.getUTCDay() + 6) % 7;
  return dateKeyOf(day - weekday * DAY_MS);
}

function addValue(left, right) {
  const a = finite(left);
  const b = finite(right);
  if (a === null) return b;
  if (b === null) return a;
  return a + b;
}

/**
 * Daily series rows (`{ date, ...numbers }`, local calendar days from the
 * server, gaps already filled) summed into weeks (Monday first) or months.
 * Each period keeps the date of its first day; a field unknown on every day
 * of a period stays null. Day and hour rows stay as they are.
 */
export function rollupSeries(series, granularity = 'day') {
  const rows = Array.isArray(series) ? series : [];
  if (granularity === 'day' || granularity === 'hour') return rows;
  const periods = new Map();
  for (const row of rows) {
    const key = periodKey(row?.date, granularity);
    let period = periods.get(key);
    if (!period) {
      period = { date: key };
      periods.set(key, period);
    }
    for (const [field, value] of Object.entries(row)) {
      if (field === 'date') continue;
      if (typeof value === 'number' || value === null) {
        period[field] = addValue(period[field], value);
      }
    }
  }
  return [...periods.values()];
}

const TREND_METRICS = Object.freeze({
  cost: Object.freeze({
    segments: Object.freeze([
      ['reported', (row) => row.reported_cost_usd],
      ['estimated', (row) => row.estimated_cost_usd],
    ]),
    total: (row) => row.cost_usd,
  }),
  tokens: Object.freeze({
    segments: Object.freeze([
      ['input', (row) => row.input_tokens],
      ['output', (row) => row.output_tokens],
    ]),
    total: (row) => totalTokens(row),
  }),
  runs: Object.freeze({
    integer: true,
    segments: Object.freeze([
      [
        'finished',
        (row) => toFiniteNumber(row.runs) - toFiniteNumber(row.failed_runs),
      ],
      ['failed', (row) => row.failed_runs],
    ]),
    total: (row) => row.runs,
  }),
});

export const TREND_METRIC_IDS = Object.freeze(Object.keys(TREND_METRICS));

/** The segment ids a trend metric stacks, bottom first. */
export function trendSegments(metric) {
  return (TREND_METRICS[metric] ?? TREND_METRICS.cost).segments.map(
    ([id]) => id,
  );
}

/**
 * Chart columns of a series for one metric (`cost`, `tokens`, `runs`):
 * each period's total, its stacked segments, and the source row; plus the
 * axis maximum. A column's key is its date, or for `hour` its row's
 * `hour_start` (the canonical UTC timestamp of the hour).
 */
export function trendColumns(series, metric = 'cost', granularity = 'day') {
  const definition = TREND_METRICS[metric] ?? TREND_METRICS.cost;
  const columns = rollupSeries(series, granularity).map((row) => {
    const segments = definition.segments.map(([id, value]) => ({
      id,
      value: Math.max(0, toFiniteNumber(value(row))),
    }));
    const segmentSum = segments.reduce((sum, entry) => sum + entry.value, 0);
    return {
      key: granularity === 'hour' ? row.hour_start : row.date,
      row,
      total: Math.max(toFiniteNumber(definition.total(row)), segmentSum),
      segments,
    };
  });
  const max = columns.reduce((top, column) => Math.max(top, column.total), 0);
  return {
    columns,
    scaleMax: niceScaleMax(max, { integer: Boolean(definition.integer) }),
  };
}

/** The trend period that keeps a series of `dayCount` days readable: days
 *  up to four months, then weeks, then months beyond about thirteen. */
export function autoGranularity(dayCount) {
  if (dayCount > 400) return 'month';
  if (dayCount > 120) return 'week';
  return 'day';
}

/** A trend axis tick: compact dollars, compact tokens or a count. */
export function formatChartTick(value, metric = 'cost', locale = 'en') {
  const number = toFiniteNumber(value);
  if (metric === 'cost') {
    return numberFormat(locale, {
      style: 'currency',
      currency: 'USD',
      notation: 'compact',
      maximumFractionDigits: number > 0 && number < 1 ? 2 : 1,
    }).format(number);
  }
  if (metric === 'tokens') return formatTokens(number, locale);
  return numberFormat(locale, { maximumFractionDigits: 1 }).format(number);
}

/** Up to `count` column indices for axis labels: every column when they
 *  fit, else columns at one regular stride that always includes the last. */
export function axisLabelIndices(length, count = 8) {
  if (length <= 0) return [];
  if (length <= count) return Array.from({ length }, (_, index) => index);
  if (count <= 1) return [length - 1];
  const stride = Math.ceil((length - 1) / (count - 1));
  const indices = [];
  for (let index = length - 1; index >= 0; index -= stride) {
    indices.unshift(index);
  }
  return indices;
}

const seriesDateFormats = new Map();

/** A series key as an axis or table label: a local calendar date as `Sep 3`
 *  for a day, the week's Monday for a week, `Sep 2026` for a month (`long`
 *  adds the year to days and weeks); an hour's UTC start timestamp as its
 *  time in the Settings time zone, `9:00 AM` (`long` adds the date). */
export function formatSeriesDate(
  dateKey,
  granularity = 'day',
  locale = 'en',
  { long = false } = {},
) {
  if (granularity === 'hour') {
    const hour = parseIso(dateKey);
    if (hour === null) return String(dateKey ?? EM_DASH);
    return formatDateTimeInApplicationZone(hour, locale, {
      ...(long ? { month: 'short', day: 'numeric' } : {}),
      hour: 'numeric',
      minute: '2-digit',
    });
  }
  const day = parseDateKey(dateKey);
  if (day === null) return String(dateKey ?? EM_DASH);
  const options =
    granularity === 'month'
      ? { month: long ? 'long' : 'short', year: 'numeric' }
      : long
        ? { month: 'short', day: 'numeric', year: 'numeric' }
        : { month: 'short', day: 'numeric' };
  const formatKey = `${locale}|${JSON.stringify(options)}`;
  let format = seriesDateFormats.get(formatKey);
  if (!format) {
    format = new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' });
    seriesDateFormats.set(formatKey, format);
  }
  const text = format.format(new Date(day));
  return granularity === 'week' && long
    ? t('statistics.chart.weekOf', { date: text })
    : text;
}

/** Axis label of a Run duration bucket: `≤10s` ... `>1h`. */
export function durationBucketLabel(upperMs, previousUpperMs) {
  const upper = finite(upperMs);
  if (upper === null) {
    const previous = finite(previousUpperMs);
    return previous === null ? EM_DASH : `>${formatDurationShort(previous)}`;
  }
  return `≤${formatDurationShort(upper)}`;
}

/**
 * Columns of the Run duration distribution: one per bucket, stacked by
 * origin in RUN_ORIGINS order, with the origins that occur at all.
 */
export function durationColumns(buckets) {
  const rows = Array.isArray(buckets) ? buckets : [];
  const present = new Set();
  for (const bucket of rows) {
    for (const [origin, count] of Object.entries(bucket?.by_origin ?? {})) {
      if (toFiniteNumber(count) > 0) present.add(origin);
    }
  }
  const origins = [
    ...RUN_ORIGINS.filter((origin) => present.has(origin)),
    ...[...present].filter((origin) => !RUN_ORIGINS.includes(origin)).sort(),
  ];
  const columns = rows.map((bucket, index) => {
    const segments = origins.map((origin) => ({
      id: origin,
      value: Math.max(0, toFiniteNumber(bucket?.by_origin?.[origin])),
    }));
    return {
      key: String(bucket?.upper_ms ?? 'more'),
      label: durationBucketLabel(bucket?.upper_ms, rows[index - 1]?.upper_ms),
      total: segments.reduce((sum, entry) => sum + entry.value, 0),
      segments,
    };
  });
  const max = columns.reduce((top, column) => Math.max(top, column.total), 0);
  return {
    columns,
    origins,
    scaleMax: niceScaleMax(max, { integer: true }),
  };
}

/** Errors per local hour of the day, all 24 hours. */
export function hourColumns(byHour) {
  const counts = new Array(24).fill(0);
  for (const entry of Array.isArray(byHour) ? byHour : []) {
    const hour = finite(entry?.hour);
    if (hour !== null && hour >= 0 && hour < 24) {
      counts[Math.floor(hour)] += toFiniteNumber(entry.count);
    }
  }
  const columns = counts.map((count, hour) => ({
    key: String(hour),
    label: formatHourLabel(hour),
    total: count,
    segments: [{ id: 'errors', value: count }],
  }));
  const max = Math.max(0, ...counts);
  return { columns, scaleMax: niceScaleMax(max, { integer: true }) };
}

/** Bar list entries: each entry's value as a fraction of the largest. */
export function barEntries(entries, value = (entry) => entry.count) {
  const rows = Array.isArray(entries) ? entries : [];
  const max = rows.reduce(
    (top, entry) => Math.max(top, toFiniteNumber(value(entry))),
    0,
  );
  return rows.map((entry) => ({
    entry,
    value: toFiniteNumber(value(entry)),
    fraction: max > 0 ? toFiniteNumber(value(entry)) / max : 0,
  }));
}
