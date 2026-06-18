// Pure display/formatting helpers for the Statistics tab. All non-trivial
// presentation logic lives here so the Svelte component stays display-only and
// this layer can be unit-tested in isolation. Locale-aware number formatting
// takes the active UI locale (`activeLocaleTag()` from i18n.js) so dates and
// numbers follow the app language, never the implicit browser locale.

import { parseAgentAddress } from './agentAddress.js';

export const STATISTICS_SUB_VIEWS = Object.freeze([
  'overview',
  'usage',
  'runs',
  'tools',
  'limits',
]);

export const DAILY_GRANULARITIES = Object.freeze(['day', 'week', 'month']);

// Percent-used thresholds at which a provider usage window turns warn / critical.
export const USAGE_SEVERITY_THRESHOLDS = Object.freeze({
  warn: 75,
  critical: 90,
});

const EM_DASH = '—';
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

function toFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function formatInteger(value, locale = 'en') {
  return new Intl.NumberFormat(locale).format(
    Math.round(toFiniteNumber(value)),
  );
}

// Tokens are plain grouped integers today; kept distinct from formatInteger so a
// future compact form (1.2k) only has to change here.
export function formatTokens(value, locale = 'en') {
  return formatInteger(value, locale);
}

export function formatPercent(ratio, { fractionDigits = 1 } = {}) {
  if (ratio == null || !Number.isFinite(ratio)) {
    return EM_DASH;
  }
  return `${(ratio * 100).toFixed(fractionDigits)}%`;
}

export function formatShare(value, total, options = {}) {
  const numericTotal = toFiniteNumber(total);
  if (numericTotal <= 0) {
    return formatPercent(0, options);
  }
  return formatPercent(toFiniteNumber(value) / numericTotal, options);
}

export function formatDurationMs(milliseconds) {
  if (milliseconds == null || !Number.isFinite(milliseconds)) {
    return EM_DASH;
  }
  const value = Math.max(0, milliseconds);
  if (value < 1000) {
    return `${Math.round(value)} ms`;
  }
  if (value < 60000) {
    return `${(value / 1000).toFixed(1)} s`;
  }
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

export function formatDateTime(isoString, locale = 'en') {
  const date = parseIso(isoString);
  if (date === null) {
    return EM_DASH;
  }
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date);
}

export function formatDate(isoString, locale = 'en') {
  const date = parseIso(isoString);
  if (date === null) {
    return EM_DASH;
  }
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(date);
}

export function formatHourLabel(hour) {
  const safeHour = Math.max(0, Math.min(23, Math.round(toFiniteNumber(hour))));
  return `${String(safeHour).padStart(2, '0')}:00`;
}

// Clamp a provider usage percentage to [0, 100] so a bar width and severity can
// never run off the track even if a provider over-reports.
export function clampUsagePercent(value) {
  return Math.max(0, Math.min(100, toFiniteNumber(value)));
}

// Map a usage percentage to a severity bucket for the bar color.
export function usageSeverity(percent) {
  const value = clampUsagePercent(percent);
  if (value >= USAGE_SEVERITY_THRESHOLDS.critical) {
    return 'critical';
  }
  if (value >= USAGE_SEVERITY_THRESHOLDS.warn) {
    return 'warn';
  }
  return 'ok';
}

// Build a relative ("3h 12m") + absolute reset-time model for a usage window.
// `now` is injectable so the relative part is deterministic in tests. Returns
// null for a missing / unparseable timestamp so the component can omit it.
export function formatResetAt(isoString, locale = 'en', now = Date.now()) {
  const date = parseIso(isoString);
  if (date === null) {
    return null;
  }
  const deltaMs = date.getTime() - now;
  return {
    absolute: formatDateTime(isoString, locale),
    relative: deltaMs > 0 ? formatRelativeDuration(deltaMs) : null,
    isPast: deltaMs <= 0,
  };
}

// Compact "2d 4h" / "3h 12m" / "45m" / "<1m" duration for a future instant.
// Shows at most the two largest non-zero units.
function formatRelativeDuration(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) {
    return null;
  }
  if (milliseconds < MINUTE_MS) {
    return '<1m';
  }
  const days = Math.floor(milliseconds / DAY_MS);
  const hours = Math.floor((milliseconds % DAY_MS) / HOUR_MS);
  const minutes = Math.floor((milliseconds % HOUR_MS) / MINUTE_MS);
  if (days > 0) {
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  }
  if (hours > 0) {
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  return `${minutes}m`;
}

function parseIso(isoString) {
  if (typeof isoString !== 'string' || isoString.length === 0) {
    return null;
  }
  const date = new Date(isoString);
  return Number.isNaN(date.getTime()) ? null : date;
}

// Measured and estimated tokens are NEVER merged into one authoritative number;
// this returns both halves plus a flag so the UI can badge the estimated part.
export function tokenSplit(record) {
  const measured =
    toFiniteNumber(record?.measured_input_tokens) +
    toFiniteNumber(record?.measured_output_tokens);
  const estimated =
    toFiniteNumber(record?.estimated_input_tokens) +
    toFiniteNumber(record?.estimated_output_tokens);
  return {
    measured,
    estimated,
    total: measured + estimated,
    hasEstimated: estimated > 0,
    hasMeasured: measured > 0,
  };
}

// Split a statistics `agent_id` into display parts. The `statistics.report`
// keys project agents as `agent@projekt` (and identity agents as a bare id), so
// every agent cell parses the address once and renders the bare name plus, for a
// project agent, a small project badge — instead of the raw `builder@vbot`
// string. An identity agent (no `@`) gets `projectId: null`, so the component
// renders it exactly as before (no badge), keeping the identity display
// byte-identical.
export function agentDisplay(agentId) {
  const { agentId: bareId, projectId } = parseAgentAddress(agentId);
  return { name: bareId, projectId };
}

export function topN(list, count) {
  if (!Array.isArray(list)) {
    return [];
  }
  return list.slice(0, Math.max(0, count));
}

// Group the flat per-model usage list under its provider for the Usage table.
// Returns providers sorted by combined token volume descending, each with its
// models in the order received (the report already sorts models by volume).
export function groupModelsByProvider(models) {
  if (!Array.isArray(models)) {
    return [];
  }
  const byProvider = new Map();
  for (const model of models) {
    const provider = model?.provider ?? 'unknown';
    if (!byProvider.has(provider)) {
      byProvider.set(provider, { provider, models: [], totalTokens: 0 });
    }
    const group = byProvider.get(provider);
    group.models.push(model);
    group.totalTokens += toFiniteNumber(model?.total_tokens);
  }
  return [...byProvider.values()].sort(
    (left, right) => right.totalTokens - left.totalTokens,
  );
}

// Roll the day-granularity series up to week (ISO Monday) or month buckets,
// summing every numeric field. 'day' returns the series unchanged. Each point
// must carry a `date` of the shape 'YYYY-MM-DD'.
export function rollupDaily(points, granularity = 'day') {
  if (!Array.isArray(points)) {
    return [];
  }
  if (granularity === 'day' || !DAILY_GRANULARITIES.includes(granularity)) {
    return points.map((point) => ({ ...point }));
  }

  const buckets = new Map();
  for (const point of points) {
    const bucketKey = bucketKeyFor(point?.date, granularity);
    if (bucketKey === null) {
      continue;
    }
    if (!buckets.has(bucketKey)) {
      buckets.set(bucketKey, { date: bucketKey });
    }
    const bucket = buckets.get(bucketKey);
    for (const [key, value] of Object.entries(point)) {
      if (key === 'date') {
        continue;
      }
      if (typeof value === 'number' && Number.isFinite(value)) {
        bucket[key] = toFiniteNumber(bucket[key]) + value;
      }
    }
  }
  return [...buckets.values()].sort((left, right) =>
    left.date < right.date ? -1 : left.date > right.date ? 1 : 0,
  );
}

function bucketKeyFor(dateString, granularity) {
  if (typeof dateString !== 'string' || dateString.length < 7) {
    return null;
  }
  if (granularity === 'month') {
    return dateString.slice(0, 7);
  }
  // week → the Monday of that ISO week, as a 'YYYY-MM-DD' string.
  const date = new Date(`${dateString}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  const dayOfWeek = (date.getUTCDay() + 6) % 7; // Monday = 0
  date.setUTCDate(date.getUTCDate() - dayOfWeek);
  return date.toISOString().slice(0, 10);
}

// Build a `points="x,y …"` attribute for an SVG sparkline polyline. Values map
// left→right across `width`; the largest value touches the top of `height`.
export function sparklinePoints(values, width, height) {
  if (!Array.isArray(values) || values.length === 0) {
    return '';
  }
  const numeric = values.map(toFiniteNumber);
  if (numeric.length === 1) {
    return `0,${height} ${width},${height - barFraction(numeric[0], numeric[0]) * height}`;
  }
  const max = Math.max(...numeric, 0);
  return numeric
    .map((value, index) => {
      const x = (index / (numeric.length - 1)) * width;
      const y = height - barFraction(value, max) * height;
      return `${round(x)},${round(y)}`;
    })
    .join(' ');
}

function barFraction(value, max) {
  return max > 0 ? Math.max(0, value) / max : 0;
}

function round(value) {
  return Math.round(value * 100) / 100;
}

// Scale a list of bar values to [0,1] fractions of the largest value, so the
// component can size bars without re-deriving the max.
export function barFractions(values) {
  if (!Array.isArray(values) || values.length === 0) {
    return [];
  }
  const numeric = values.map(toFiniteNumber);
  const max = Math.max(...numeric, 0);
  return numeric.map((value) => barFraction(value, max));
}

// Donut segments for the run-status ring: each segment carries its fraction of
// the whole and the cumulative fraction before it (for stroke-dashoffset). Zero
// total yields no segments so the component can render an empty ring.
export function donutSegments(parts) {
  const entries = Array.isArray(parts) ? parts : [];
  const total = entries.reduce(
    (sum, part) => sum + toFiniteNumber(part?.value),
    0,
  );
  if (total <= 0) {
    return [];
  }
  let cumulative = 0;
  const segments = [];
  for (const part of entries) {
    const value = toFiniteNumber(part?.value);
    if (value <= 0) {
      continue;
    }
    const fraction = value / total;
    segments.push({
      key: part?.key ?? '',
      value,
      fraction,
      offset: cumulative,
    });
    cumulative += fraction;
  }
  return segments;
}
