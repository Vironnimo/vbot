// Pure presentation rules of the Statistics view (StatisticsView.svelte and
// components/statistics/): the report sections each tab requests, range
// windows in the application time zone, number, token and cost formatting,
// changes against the previous period, chart columns, and the plain-language
// lines and tooltips behind the numbers. Locale-aware formatting takes the
// active UI locale (`activeLocaleTag()`), never the implicit browser locale.
// The Limits tab's helpers live in statisticsLimits.js.

import { parseAgentAddress } from './agentAddress.js';
import {
  dateKeyInApplicationZone,
  formatDateTimeInApplicationZone,
} from './dateTimePrefs.svelte.js';
import { t, tOr } from './i18n.js';

const EM_DASH = '—';
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

// ---------------------------------------------------------------------------
// Tabs, sections and ranges

export const STATISTICS_TABS = Object.freeze([
  'overview',
  'usage',
  'runs',
  'tools',
  'limits',
  'extensions',
  'diagnostics',
]);

// The `statistics.report` sections one tab renders, requested in one call.
// Limits reads live provider usage instead of the report.
const TAB_SECTIONS = Object.freeze({
  overview: Object.freeze(['overview']),
  usage: Object.freeze(['usage']),
  runs: Object.freeze(['runs']),
  tools: Object.freeze(['tools', 'skills']),
  extensions: Object.freeze(['extensions']),
  diagnostics: Object.freeze(['diagnostics']),
});

// Places of retired tabs open the tab that now holds their content.
const RETIRED_PLACES = Object.freeze({
  compactions: 'diagnostics',
  skills: 'tools',
});

export const STATISTICS_RANGES = Object.freeze([
  '24h',
  '7d',
  '30d',
  '90d',
  'all',
]);
export const DEFAULT_STATISTICS_RANGE = '30d';
const RANGE_DAYS = Object.freeze({ '7d': 7, '30d': 30, '90d': 90 });

/** The report sections a tab requests; none for Limits. */
export function statisticsTabSections(tab) {
  return Object.hasOwn(TAB_SECTIONS, tab) ? TAB_SECTIONS[tab] : [];
}

/** The tab a navigation place shows: a tab id, a retired tab's successor,
 *  or the Overview. */
export function statisticsTabForPlace(place) {
  if (STATISTICS_TABS.includes(place)) return place;
  return Object.hasOwn(RETIRED_PLACES, place)
    ? RETIRED_PLACES[place]
    : STATISTICS_TABS[0];
}

const TAB_LABELS = Object.freeze({
  overview: () => t('statistics.subview.overview'),
  usage: () => t('statistics.subview.usage'),
  runs: () => t('statistics.subview.runs'),
  tools: () => t('statistics.subview.tools'),
  limits: () => t('statistics.subview.limits'),
  extensions: () => t('statistics.subview.extensions'),
  diagnostics: () => t('statistics.subview.diagnostics'),
});

/** A tab's name. */
export function statisticsTabLabel(tab) {
  return (TAB_LABELS[tab] ?? TAB_LABELS.overview)();
}

export function isStatisticsRange(value) {
  return STATISTICS_RANGES.includes(value);
}

const zoneFormatters = new Map();

function zoneFormatter(timeZone) {
  let formatter = zoneFormatters.get(timeZone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
    });
    zoneFormatters.set(timeZone, formatter);
  }
  return formatter;
}

function zonedParts(instant, timeZone) {
  let formatter;
  try {
    formatter = zoneFormatter(timeZone || 'UTC');
  } catch {
    formatter = zoneFormatter('UTC');
  }
  const parts = Object.fromEntries(
    formatter
      .formatToParts(new Date(instant))
      .map((part) => [part.type, Number(part.value)]),
  );
  return parts;
}

// Milliseconds the zone's wall clock runs ahead of UTC at `instant`.
function zoneOffset(instant, timeZone) {
  const parts = zonedParts(instant, timeZone);
  const wall = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
  );
  return wall - Math.floor(instant / 1000) * 1000;
}

// The instant of local midnight on a calendar day in `timeZone`; the second
// pass corrects a day whose offset differs from midnight UTC's (DST).
function zonedMidnight(year, monthIndex, day, timeZone) {
  const wall = Date.UTC(year, monthIndex, day);
  const first = wall - zoneOffset(wall, timeZone);
  return wall - zoneOffset(first, timeZone);
}

/**
 * The report window of a range: `24h` is the rolling last day, `7d`, `30d`
 * and `90d` are that many calendar days in `timeZone` including today, and
 * `all` has no start. The server floors `since` to the hour.
 */
export function statisticsWindow(range, timeZone = 'UTC', now = Date.now()) {
  if (range === '24h') {
    return { since: new Date(now - DAY_MS).toISOString() };
  }
  const days = RANGE_DAYS[range];
  if (!days) return {};
  const today = zonedParts(now, timeZone);
  const start = zonedMidnight(
    today.year,
    today.month - 1,
    today.day - (days - 1),
    timeZone,
  );
  return { since: new Date(start).toISOString() };
}

/** The `statistics.report` parameters of one tab, range and time zone. */
export function statisticsReportParams(
  tab,
  range,
  timeZone = 'UTC',
  now = Date.now(),
) {
  return {
    ...statisticsWindow(range, timeZone, now),
    timezone: timeZone,
    sections: [...statisticsTabSections(tab)],
  };
}

/** The cache key of one tab's report: its sections, range and time zone. */
export function statisticsReportKey(tab, range, timeZone = 'UTC') {
  return `${statisticsTabSections(tab).join('+')}|${range}|${timeZone}`;
}

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

/** A duration: `850 ms`, `12.3 s`, `4m 05s`, `2h 10m`; a dash when unknown. */
export function formatDurationMs(milliseconds) {
  const value = finite(milliseconds);
  if (value === null) return EM_DASH;
  const ms = Math.max(0, value);
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < MINUTE_MS) return `${(ms / 1000).toFixed(1)} s`;
  if (ms < HOUR_MS) {
    const minutes = Math.floor(ms / MINUTE_MS);
    const seconds = Math.round((ms % MINUTE_MS) / 1000);
    return seconds === 60
      ? `${minutes + 1}m 00s`
      : `${minutes}m ${String(seconds).padStart(2, '0')}s`;
  }
  const hours = Math.floor(ms / HOUR_MS);
  const minutes = Math.round((ms % HOUR_MS) / MINUTE_MS);
  return `${hours}h ${minutes}m`;
}

// Short duration for chart axes: 10s, 2m, 1h.
function formatDurationShort(milliseconds) {
  if (milliseconds >= HOUR_MS) return `${Math.round(milliseconds / HOUR_MS)}h`;
  if (milliseconds >= MINUTE_MS) {
    return `${Math.round(milliseconds / MINUTE_MS)}m`;
  }
  return `${Math.round(milliseconds / 1000)}s`;
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

/** Cache hit rate: cache reads as a share of the input of the calls that
 *  report a cache counter; null when no call reports one. */
export function cacheHitRate(record) {
  if (record?.cache_calls === 0) return null;
  const input = toFiniteNumber(record?.cache_input_tokens);
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

// ---------------------------------------------------------------------------
// Changes against the previous period

/**
 * The change of a value against the previous period of equal length, or null
 * when either value is unknown. `kind: 'relative'` is the percent change of
 * the value, `kind: 'points'` the difference of two ratios in percentage
 * points. `judge: 'lowerIsBetter'` marks a rise as bad and a fall as good;
 * without it the change stays neutral (more cost is not a verdict).
 */
export function periodChange(
  current,
  previous,
  { kind = 'relative', judge = null, locale = 'en' } = {},
) {
  const now = finite(current);
  const before = finite(previous);
  if (now === null || before === null) return null;
  let text;
  let direction;
  if (kind === 'points') {
    const points = Math.round((now - before) * 1000) / 10;
    direction = points > 0 ? 'up' : points < 0 ? 'down' : 'flat';
    text = t('statistics.change.points', {
      value: numberFormat(locale, {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      }).format(Math.abs(points)),
    });
  } else if (before === 0) {
    if (now === 0) {
      direction = 'flat';
      text = formatPercent(0, locale);
    } else {
      direction = now > 0 ? 'up' : 'down';
      text = t('statistics.change.new');
    }
  } else {
    const ratio = (now - before) / Math.abs(before);
    const rounded = Math.round(ratio * 1000) / 1000;
    direction = rounded > 0 ? 'up' : rounded < 0 ? 'down' : 'flat';
    text = formatPercent(Math.abs(rounded), locale);
  }
  let tone = 'neutral';
  if (judge === 'lowerIsBetter' && direction !== 'flat') {
    tone = direction === 'up' ? 'bad' : 'good';
  }
  return { direction, text, tone };
}

// ---------------------------------------------------------------------------
// Names and labels

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

/** Who started a Run or Model call (`user`, `subagent`, ...), translated. */
export function originLabel(origin) {
  return tOr(`statistics.origin.${origin}`, String(origin || EM_DASH));
}

/** Run status, translated; unknown statuses stay raw. */
export function runStatusLabel(status) {
  return tOr(`statistics.status.${status}`, String(status || EM_DASH));
}

/** The Badge variant of a Run status. */
export function runStatusVariant(status) {
  switch (status) {
    case 'completed':
      return 'success';
    case 'failed':
      return 'error';
    case 'interrupted':
      return 'warn';
    case 'running':
      return 'info';
    default:
      return 'neutral';
  }
}

// Model request kinds and outcomes with a catalog label; any other value
// renders the generic label.
const MODEL_CALL_KINDS = new Set([
  'chat',
  'compaction',
  'speech_to_text',
  'text_to_speech',
  'image_understanding',
  'image_generation',
  'video_generation',
  'music_generation',
  'text_embedding',
  'decision',
  'live_voice',
  'live_voice_backend',
  'session_title',
  'group_title',
  'extension_sampling',
]);

const MODEL_CALL_STATUSES = new Set([
  'started',
  'completed',
  'failed',
  'cancelled',
  'interrupted',
]);

export function modelCallKindLabel(kind) {
  return MODEL_CALL_KINDS.has(kind)
    ? t(`statistics.kind.${kind}`)
    : t('statistics.kind.other');
}

/** An id in words: `tool_iterations_exceeded` -> `Tool iterations exceeded`. */
function humanizeId(id) {
  const words = String(id ?? '')
    .replace(/[_-]+/g, ' ')
    .trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : EM_DASH;
}

/** A recorded Run error kind (`rate_limit`, `auth_error`, ...), translated;
 *  a kind without a label reads as its id in words. */
export function errorKindLabel(kind) {
  return tOr(`statistics.errorKind.${kind}`, humanizeId(kind));
}

/** An error kind's name with its stored id as a secondary row. */
export function errorKindTooltip(kind) {
  return {
    title: errorKindLabel(kind),
    rows: [
      {
        label: t('statistics.errors.kindId'),
        value: String(kind ?? ''),
        mono: true,
      },
    ],
  };
}

export function modelCallStatusLabel(status) {
  return MODEL_CALL_STATUSES.has(status)
    ? t(`statistics.requestStatus.${status}`)
    : t('statistics.requestStatus.unknown');
}

// Report rows attribute Extension-owned Sessions (for example Swarm
// participants) to `extension:<name>`; Agent ids never contain `:`.
const EXTENSION_ACTOR_PREFIX = 'extension:';

/**
 * Display parts of a statistics Agent key: a Project Agent (`agent@project`)
 * shows its bare name with a Project badge, an Extension key
 * (`extension:<name>`) its Extension name with an Extension badge, and an
 * identity Agent its id.
 */
export function agentDisplay(agentId) {
  if (
    typeof agentId === 'string' &&
    agentId.startsWith(EXTENSION_ACTOR_PREFIX) &&
    agentId.length > EXTENSION_ACTOR_PREFIX.length
  ) {
    return {
      name: agentId.slice(EXTENSION_ACTOR_PREFIX.length),
      projectId: null,
      extension: true,
    };
  }
  const { agentId: bareId, projectId } = parseAgentAddress(agentId);
  return { name: bareId, projectId, extension: false };
}

/** Text an Agent cell is found by in a table filter. */
export function agentFilterText(agentId) {
  const display = agentDisplay(agentId);
  return [display.name, display.projectId].filter(Boolean).join(' ');
}

/** Short, stable suffix identifying an untitled Extension group. */
export function shortGroupId(groupId) {
  const value = typeof groupId === 'string' ? groupId : '';
  return value.slice(-6);
}

export const USAGE_DIMENSIONS = Object.freeze([
  'agent',
  'model',
  'provider',
  'project',
  'origin',
  'kind',
]);

const USAGE_DIMENSION_LABELS = Object.freeze({
  agent: () => t('statistics.dimension.agent'),
  model: () => t('statistics.dimension.model'),
  provider: () => t('statistics.dimension.provider'),
  project: () => t('statistics.dimension.project'),
  origin: () => t('statistics.dimension.origin'),
  kind: () => t('statistics.dimension.kind'),
});

/** The name of a Costs & tokens breakdown dimension. */
export function usageDimensionLabel(dimension) {
  return (USAGE_DIMENSION_LABELS[dimension] ?? USAGE_DIMENSION_LABELS.agent)();
}

/** A breakdown row's display text in its dimension (Agent rows render their
 *  own cell). */
export function usageRowLabel(dimension, key) {
  switch (dimension) {
    case 'project':
      return key ? key : t('statistics.usage.identityUsage');
    case 'origin':
      return originLabel(key);
    case 'kind':
      return modelCallKindLabel(key);
    case 'agent':
      return key ? agentFilterText(key) : t('statistics.cost.withoutSession');
    default:
      return key || EM_DASH;
  }
}

// ---------------------------------------------------------------------------
// Tooltips. Each builder returns what the shared `use:tooltip` action accepts
// (lib/tooltip.js): '' for nothing, a string, or `{ title, text, rows }`.
// Definitions live in InfoHints on labels and column headers; these carry
// the facts about one value.

/**
 * An Agent cell: a Project Agent names its Project and full address, an
 * Extension explains its Sessions, and a plain Agent name shows in full only
 * while it is truncated.
 */
export function agentTooltip(agentId) {
  const display = agentDisplay(agentId);
  if (display.extension) {
    return {
      title: display.name,
      text: t('statistics.agent.extensionBadgeTitle'),
    };
  }
  if (display.projectId) {
    return {
      title: display.name,
      rows: [
        { label: t('statistics.agent.project'), value: display.projectId },
        {
          label: t('statistics.agent.address'),
          value: agentId,
          mono: true,
        },
      ],
    };
  }
  return { text: display.name, whenTruncated: true };
}

/**
 * A Session cell names the Session by its title and keeps the id as a
 * secondary row; an untitled Session shows its id, in full only while
 * truncated.
 */
export function sessionTooltip(row) {
  const id = typeof row?.session_id === 'string' ? row.session_id : '';
  if (row?.session_title) {
    return {
      title: row.session_title,
      rows: [{ label: t('statistics.sessionId'), value: id, mono: true }],
    };
  }
  return { text: id, mono: true, selectable: true, whenTruncated: true };
}

/** A compact token value's exact count, with the estimated part when some
 *  calls reported no usage. */
export function tokenTooltip(value, estimated, locale = 'en') {
  const exact = formatTokensExact(value, locale);
  if (toFiniteNumber(estimated) <= 0) return exact;
  return {
    title: exact,
    rows: [
      {
        label: t('statistics.tokens.estimatedPart'),
        value: formatTokensExact(estimated, locale),
        tone: 'warning',
      },
    ],
  };
}

/**
 * The parts of a cost: the Provider-reported and catalog-estimated amounts
 * with the calls they cover, the calls without a price, and the estimates
 * priced with today's catalog.
 */
export function costTooltip(totals, locale = 'en') {
  if (!totals) return '';
  const rows = [];
  const callCount = (count) =>
    t('statistics.cost.callCount', { count: formatInteger(count, locale) });
  if (toFiniteNumber(totals.reported_calls) > 0) {
    rows.push({
      label: t('statistics.cost.reported'),
      value: `${formatCostExact(totals.reported_cost_usd, locale)} · ${callCount(totals.reported_calls)}`,
    });
  }
  if (toFiniteNumber(totals.estimated_calls) > 0) {
    rows.push({
      label: t('statistics.cost.estimated'),
      value: `${formatCostExact(totals.estimated_cost_usd, locale)} · ${callCount(totals.estimated_calls)}`,
    });
  }
  if (toFiniteNumber(totals.unpriced_calls) > 0) {
    rows.push({
      label: t('statistics.cost.unpriced'),
      value: callCount(totals.unpriced_calls),
      tone: 'warning',
    });
  }
  if (toFiniteNumber(totals.retrospective_calls) > 0) {
    rows.push({
      label: t('statistics.cost.retrospective'),
      value: callCount(totals.retrospective_calls),
    });
  }
  if (rows.length === 0) return '';
  return { title: formatCostExact(totals.cost_usd, locale), rows };
}

const COST_REASONS = Object.freeze({
  missing_usage: () => t('statistics.cost.reason.usage'),
  missing_price: () => t('statistics.cost.reason.price'),
  unsupported_tier: () => t('statistics.cost.reason.tier'),
  invalid_cache: () => t('statistics.cost.reason.cache'),
  invalid_reasoning: () => t('statistics.cost.reason.reasoning'),
  missing_reasoning_usage: () => t('statistics.cost.reason.reasoningUsage'),
  missing_bucket_price: () => t('statistics.cost.reason.bucket'),
});

const PRICE_RATES = Object.freeze([
  ['input', () => t('statistics.cost.rate.input')],
  ['output', () => t('statistics.cost.rate.output')],
  ['cache_read', () => t('statistics.cost.rate.cacheRead')],
  ['cache_write', () => t('statistics.cost.rate.cacheWrite')],
  ['reasoning', () => t('statistics.cost.rate.reasoning')],
]);

/** How one Model call's cost was determined: its price source, the reason
 *  it has none, and the catalog rates used. */
export function callCostTooltip(call, locale = 'en') {
  const cost = call?.cost ?? {};
  const rows = [];
  let title;
  if (cost.source === 'provider') {
    title = t('statistics.cost.providerSource');
  } else if (cost.source === 'catalog') {
    title = call?.retrospective
      ? t('statistics.cost.currentCatalog')
      : t('statistics.cost.savedCatalog');
    const rates = cost.pricing?.rates ?? {};
    for (const [key, label] of PRICE_RATES) {
      if (finite(rates[key]) !== null) {
        rows.push({
          label: label(),
          value: t('statistics.cost.perMillion', {
            price: formatCostExact(rates[key], locale),
          }),
        });
      }
    }
    if (Array.isArray(rates.tiers) && rates.tiers.length > 0) {
      rows.push({
        label: t('statistics.cost.tiers'),
        value: formatInteger(rates.tiers.length, locale),
      });
    }
    if (cost.pricing?.source) {
      rows.push({
        label: t('statistics.cost.source'),
        value: String(cost.pricing.source),
      });
    }
  } else {
    title = t('statistics.cost.unknown');
    const reason = Object.hasOwn(COST_REASONS, cost.reason)
      ? COST_REASONS[cost.reason]()
      : '';
    if (reason)
      rows.push({ label: t('statistics.cost.reason'), value: reason });
  }
  if (call?.estimated_tokens === true) {
    rows.push({
      label: t('statistics.col.tokens'),
      value: t('statistics.tokens.estimatedPart'),
      tone: 'warning',
    });
  }
  return { title, rows };
}

/** A Tool's most frequent rejection codes. */
export function toolRejectionTooltip(tool, locale = 'en') {
  const codes = Array.isArray(tool?.top_codes) ? tool.top_codes : [];
  if (codes.length === 0) return '';
  return {
    title: t('statistics.tools.rejectionsOf', {
      count: formatInteger(tool.rejected, locale),
      calls: formatInteger(tool.calls, locale),
    }),
    rows: codes.map((entry) => ({
      label: String(entry.code),
      value: formatInteger(entry.count, locale),
      mono: true,
    })),
  };
}

// ---------------------------------------------------------------------------
// Skills

// Origin scopes that carry a detail after a colon (`agent:<id>`,
// `project:<name>`). `bundled` / `global` are bare scope tokens.
const SCOPED_ORIGIN_PREFIXES = Object.freeze(['agent', 'project']);

/**
 * Split a Skill origin (`bundled`, `global`, `agent:<id>`,
 * `project:<name>`) into `{ scope, detail }`; only the first colon
 * separates, and bare or unknown tokens have a null detail.
 */
export function parseOrigin(origin) {
  const raw = typeof origin === 'string' ? origin.trim() : '';
  if (!raw) return { scope: '', detail: null };
  const separatorIndex = raw.indexOf(':');
  if (separatorIndex === -1) return { scope: raw, detail: null };
  const scope = raw.slice(0, separatorIndex);
  const detail = raw.slice(separatorIndex + 1);
  if (!SCOPED_ORIGIN_PREFIXES.includes(scope) || detail.length === 0) {
    return { scope: raw, detail: null };
  }
  return { scope, detail };
}

/** A Skill origin, translated. */
export function skillOriginLabel(origin) {
  const { scope, detail } = parseOrigin(origin);
  if (detail !== null) {
    return t(`statistics.skills.scopedOrigin.${scope}`, { detail });
  }
  return tOr(`statistics.skills.origin.${scope}`, scope);
}

/** The evidence behind a Skill's offer conversion. */
export function skillConversionTooltip(skill, locale = 'en') {
  const offered = toFiniteNumber(skill?.offered_sessions);
  if (offered === 0) return t('statistics.skills.noOfferDataRowTitle');
  const converted = toFiniteNumber(skill?.activated_offered_sessions);
  const outside = toFiniteNumber(skill?.activated_sessions) - converted;
  return {
    text: t('statistics.skills.conversionDetail', {
      activated: formatInteger(converted, locale),
      offered: formatInteger(offered, locale),
    }),
    rows:
      outside > 0
        ? [
            {
              label: t('statistics.skills.activatedWithoutOffer'),
              value: formatInteger(outside, locale),
            },
          ]
        : [],
  };
}

/** Skills offered in Sessions that never activated them: candidates to
 *  improve or remove. */
export function skillCandidates(skills) {
  return (Array.isArray(skills) ? skills : []).filter(
    (skill) =>
      toFiniteNumber(skill?.offered_sessions) > 0 &&
      toFiniteNumber(skill?.activated_offered_sessions) === 0,
  );
}

// ---------------------------------------------------------------------------
// Compactions

// Stored Compaction strategy ids and the Compaction mode names Settings uses.
const COMPACTION_STRATEGIES = Object.freeze({
  summary_tail: () => ({
    label: t('compaction.strategy.summaryTail'),
    description: t('compaction.strategy.summaryTailDescription'),
  }),
  continuation: () => ({
    label: t('compaction.strategy.continuation'),
    description: t('compaction.strategy.continuationDescription'),
  }),
});

function compactionStrategy(strategy) {
  return Object.hasOwn(COMPACTION_STRATEGIES, strategy)
    ? COMPACTION_STRATEGIES[strategy]()
    : null;
}

/** The Compaction mode name for a stored strategy id; unknown ids stay raw. */
export function compactionStrategyLabel(strategy) {
  return compactionStrategy(strategy)?.label ?? String(strategy ?? '');
}

/** What a Compaction mode does, with its stored id as a secondary row. */
export function compactionStrategyTooltip(strategy) {
  const known = compactionStrategy(strategy);
  if (!known) return '';
  return {
    title: known.label,
    text: known.description,
    rows: [
      {
        label: t('statistics.compactions.strategyId'),
        value: strategy,
        mono: true,
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Insights

const INSIGHT_TABS = Object.freeze({
  uncached_value: 'usage',
  cancelled_cost: 'runs',
  top_runs_share: 'runs',
  failed_attempt_burst: 'diagnostics',
  runaway_runs: 'diagnostics',
  tool_failure: 'tools',
});

function insightText(id, values, locale) {
  switch (id) {
    case 'uncached_value':
      return values.top_model
        ? t('statistics.insight.uncachedValueModel', {
            share: formatPercent(values.share, locale),
            cost: formatCost(values.cost_usd, locale),
            model: values.top_model,
          })
        : t('statistics.insight.uncachedValue', {
            share: formatPercent(values.share, locale),
            cost: formatCost(values.cost_usd, locale),
          });
    case 'cancelled_cost':
      return t('statistics.insight.cancelledCost', {
        runs: formatInteger(values.runs, locale),
        cost: formatCost(values.cost_usd, locale),
        share: formatPercent(values.share, locale),
      });
    case 'top_runs_share':
      return t('statistics.insight.topRunsShare', {
        runs: formatInteger(values.runs, locale),
        share: formatPercent(values.share, locale),
      });
    case 'failed_attempt_burst':
      return values.model
        ? t('statistics.insight.failedAttemptBurstModel', {
            failed: formatInteger(values.failed, locale),
            time: formatDateTime(values.hour_start, locale),
            model: values.model,
          })
        : t('statistics.insight.failedAttemptBurst', {
            failed: formatInteger(values.failed, locale),
            time: formatDateTime(values.hour_start, locale),
          });
    case 'runaway_runs':
      return t('statistics.insight.runawayRuns', {
        runs: formatInteger(values.runs, locale),
      });
    case 'tool_failure':
      return t('statistics.insight.toolFailure', {
        tool: String(values.tool ?? EM_DASH),
        rate: formatPercent(values.rate, locale),
        calls: formatInteger(values.calls, locale),
      });
    default:
      return '';
  }
}

/**
 * The Overview's "Worth a look" lines: one plain-language sentence per
 * insight the report raised, with the tab that explains it. Unknown insight
 * ids are skipped.
 */
export function insightLines(insights, locale = 'en') {
  return (Array.isArray(insights) ? insights : []).flatMap((insight) => {
    if (!Object.hasOwn(INSIGHT_TABS, insight?.id)) return [];
    const text = insightText(insight.id, insight.values ?? {}, locale);
    return text
      ? [
          {
            id: insight.id,
            severity: insight.severity === 'warn' ? 'warn' : 'info',
            text,
            tab: INSIGHT_TABS[insight.id],
          },
        ]
      : [];
  });
}

/** The insight with `id`, or null. */
export function findInsight(insights, id) {
  return (
    (Array.isArray(insights) ? insights : []).find(
      (insight) => insight?.id === id,
    ) ?? null
  );
}

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
 * of a period stays null.
 */
export function rollupSeries(series, granularity = 'day') {
  const rows = Array.isArray(series) ? series : [];
  if (granularity === 'day') return rows;
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
 * axis maximum.
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
      key: row.date,
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

/** A local calendar date key as an axis or table label: `Sep 3` for a day,
 *  the week's Monday for a week, `Sep 2026` for a month. `long` adds the
 *  year to days and weeks. */
export function formatSeriesDate(
  dateKey,
  granularity = 'day',
  locale = 'en',
  { long = false } = {},
) {
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
