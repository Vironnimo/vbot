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
  'compactions',
  'tools',
  'skills',
  'limits',
]);

export const DAILY_GRANULARITIES = Object.freeze(['day', 'week', 'month']);
export const USAGE_HISTORY_RANGES = Object.freeze(['24h', '7d', '30d', 'all']);

export const ACTIVITY_BUCKET_COUNTS = Object.freeze({
  day: 30,
  week: 16,
  month: 12,
});

const ACTIVITY_FIELDS = Object.freeze([
  'runs',
  'completed',
  'failed',
  'cancelled',
  'interrupted',
]);
const CHART_SCALE_STEPS = Object.freeze([1, 1.5, 2, 2.5, 5, 10]);

// Percent-used thresholds at which a provider usage window turns warn / critical.
const USAGE_SEVERITY_THRESHOLDS = Object.freeze({
  warn: 75,
  critical: 90,
});

const EM_DASH = '—';
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const USAGE_HISTORY_GAP_MS = 90 * MINUTE_MS;

function toFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function formatInteger(value, locale = 'en') {
  return new Intl.NumberFormat(locale).format(
    Math.round(toFiniteNumber(value)),
  );
}

export function formatChartTick(value, locale = 'en') {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(
    toFiniteNumber(value),
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
  return formatDateTimeInApplicationZone(date, locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

export function formatDate(isoString, locale = 'en') {
  const date = parseIso(isoString);
  if (date === null) {
    return EM_DASH;
  }
  return formatDateTimeInApplicationZone(date, locale, {
    dateStyle: 'medium',
  });
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

export function providerUsageTargetKey(snapshot) {
  const connection =
    typeof snapshot?.connection === 'string' ? snapshot.connection : '';
  const account =
    typeof snapshot?.account === 'string' && snapshot.account
      ? snapshot.account
      : 'default';
  return `${connection}::${account}`;
}

export function usageHistorySince(range, now = Date.now()) {
  if (range === 'all') {
    return null;
  }
  const duration =
    range === '24h' ? DAY_MS : range === '30d' ? 30 * DAY_MS : 7 * DAY_MS;
  const timestamp = Number.isFinite(now) ? now : Date.now();
  return new Date(timestamp - duration).toISOString();
}

export function buildUsageHistorySeries(samples) {
  const byKey = new Map();
  for (const sample of Array.isArray(samples) ? samples : []) {
    const sampledAt = parseIso(sample?.sampled_at);
    if (sampledAt === null || !Array.isArray(sample?.providers)) {
      continue;
    }
    for (const snapshot of sample.providers) {
      const targetKey = providerUsageTargetKey(snapshot);
      if (!targetKey.startsWith('::') && Array.isArray(snapshot?.windows)) {
        for (const window of snapshot.windows) {
          if (
            typeof window?.label !== 'string' ||
            !Number.isFinite(window?.used_percent)
          ) {
            continue;
          }
          const duration = Number.isFinite(window.window_seconds)
            ? window.window_seconds
            : null;
          const key = `${targetKey}::${window.label}::${duration ?? ''}`;
          let series = byKey.get(key);
          if (!series) {
            series = {
              key,
              targetKey,
              connection: snapshot.connection,
              account: snapshot.account || 'default',
              displayName: snapshot.display_name || snapshot.connection,
              plan: snapshot.plan ?? null,
              label: window.label,
              windowSeconds: duration,
              points: [],
            };
            byKey.set(key, series);
          }
          series.plan = snapshot.plan ?? series.plan;
          series.points.push({
            sampledAt: sample.sampled_at,
            timestamp: sampledAt.getTime(),
            usedPercent: clampUsagePercent(window.used_percent),
            resetAt:
              typeof window.reset_at === 'string' ? window.reset_at : null,
          });
        }
      }
    }
  }
  return [...byKey.values()]
    .map((series) => ({
      ...series,
      points: series.points.sort(
        (left, right) => left.timestamp - right.timestamp,
      ),
    }))
    .sort(
      (left, right) =>
        left.displayName.localeCompare(right.displayName) ||
        left.account.localeCompare(right.account) ||
        left.label.localeCompare(right.label),
    );
}

export function usageHistoryIntervals(seriesList) {
  const intervals = [];
  for (const series of Array.isArray(seriesList) ? seriesList : []) {
    const points = Array.isArray(series?.points) ? series.points : [];
    for (let index = 1; index < points.length; index += 1) {
      const previous = points[index - 1];
      const current = points[index];
      const elapsedMs = current.timestamp - previous.timestamp;
      if (elapsedMs <= 0) {
        continue;
      }
      const resetChanged =
        previous.resetAt !== current.resetAt &&
        (previous.resetAt !== null || current.resetAt !== null);
      const inferredReset = current.usedPercent < previous.usedPercent;
      const kind =
        elapsedMs > USAGE_HISTORY_GAP_MS
          ? 'gap'
          : resetChanged || inferredReset
            ? 'reset'
            : 'change';
      intervals.push({
        id: `${series.key}::${current.sampledAt}`,
        kind,
        seriesKey: series.key,
        connection: series.connection,
        account: series.account,
        displayName: series.displayName,
        label: series.label,
        from: previous,
        to: current,
        elapsedMs,
        delta:
          kind === 'change' ? current.usedPercent - previous.usedPercent : null,
      });
    }
  }
  return intervals.sort((left, right) => {
    const leftChange = left.kind === 'change' ? Math.max(0, left.delta) : -1;
    const rightChange = right.kind === 'change' ? Math.max(0, right.delta) : -1;
    return (
      rightChange - leftChange ||
      right.to.timestamp - left.to.timestamp ||
      left.id.localeCompare(right.id)
    );
  });
}

export function usageHistoryPolylineSegments(
  points,
  width = 720,
  height = 160,
) {
  const safePoints = Array.isArray(points) ? points : [];
  if (safePoints.length === 0) {
    return [];
  }
  const coordinates = usageHistoryPointCoordinates(safePoints, width, height);
  const segments = [];
  let current = [coordinates[0]];
  for (let index = 1; index < safePoints.length; index += 1) {
    const previous = safePoints[index - 1];
    const point = safePoints[index];
    const resetChanged =
      previous.resetAt !== point.resetAt &&
      (previous.resetAt !== null || point.resetAt !== null);
    const breakBefore =
      point.timestamp - previous.timestamp > USAGE_HISTORY_GAP_MS ||
      resetChanged ||
      point.usedPercent < previous.usedPercent;
    if (breakBefore) {
      segments.push(current);
      current = [];
    }
    current.push(coordinates[index]);
  }
  segments.push(current);
  return segments.map((segment) =>
    segment.map(({ x, y }) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' '),
  );
}

export function usageHistoryPointCoordinates(
  points,
  width = 720,
  height = 160,
) {
  const safePoints = Array.isArray(points) ? points : [];
  if (safePoints.length === 0) {
    return [];
  }
  const firstTimestamp = safePoints[0].timestamp;
  const lastTimestamp = safePoints.at(-1).timestamp;
  const span = Math.max(1, lastTimestamp - firstTimestamp);
  return safePoints.map((point) => ({
    x: ((point.timestamp - firstTimestamp) / span) * width,
    y: height - (clampUsagePercent(point.usedPercent) / 100) * height,
  }));
}

export function usageHistorySummary(samples) {
  const safeSamples = Array.isArray(samples) ? samples : [];
  const targets = new Set();
  let unavailable = 0;
  for (const sample of safeSamples) {
    for (const snapshot of Array.isArray(sample?.providers)
      ? sample.providers
      : []) {
      targets.add(providerUsageTargetKey(snapshot));
      if (
        snapshot?.error ||
        !Array.isArray(snapshot?.windows) ||
        snapshot.windows.length === 0
      ) {
        unavailable += 1;
      }
    }
  }
  return {
    samples: safeSamples.length,
    targets: targets.size,
    unavailable,
    firstSample: safeSamples[0]?.sampled_at ?? null,
    lastSample: safeSamples.at(-1)?.sampled_at ?? null,
  };
}

export function runActivityTotals(runs) {
  const totals = {
    runs: 0,
    measuredTokens: 0,
    estimatedTokens: 0,
  };
  for (const run of Array.isArray(runs) ? runs : []) {
    totals.runs += 1;
    totals.measuredTokens +=
      toFiniteNumber(run?.measured_input_tokens) +
      toFiniteNumber(run?.measured_output_tokens);
    totals.estimatedTokens +=
      toFiniteNumber(run?.estimated_input_tokens) +
      toFiniteNumber(run?.estimated_output_tokens);
  }
  return totals;
}

export function formatUsageDelta(value, locale = 'en') {
  if (!Number.isFinite(value)) {
    return EM_DASH;
  }
  const sign = value > 0 ? '+' : '';
  return `${sign}${new Intl.NumberFormat(locale, {
    maximumFractionDigits: 1,
  }).format(value)} pp`;
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

// Cache hit rate over a record's cache-reporting turns: cache_read_tokens as a
// share of cache_input_tokens (the input of exactly those turns). Returns null
// when the record carries no cache data at all, so a provider that never
// reports caching renders as "—" instead of a misleading 0%.
export function cacheHitRate(record) {
  const cacheInput = toFiniteNumber(record?.cache_input_tokens);
  if (cacheInput <= 0) {
    return null;
  }
  return toFiniteNumber(record?.cache_read_tokens) / cacheInput;
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

// Produce a fixed, calendar-correct activity window ending at `anchorIso`.
// The report intentionally emits only dates that contain persisted activity;
// filling absent buckets here prevents inactive days or weeks from collapsing
// out of the visual timeline. Fixed windows keep the chart legible as Session
// history grows without adding a second backend paging contract.
export function buildActivityTimeline(
  points,
  granularity = 'day',
  anchorIso = null,
) {
  const period = DAILY_GRANULARITIES.includes(granularity)
    ? granularity
    : 'day';
  const rolled = rollupDaily(points, period);
  const anchorDay = isoDayKey(anchorIso) ?? lastSeriesDay(rolled);
  const endKey = bucketKeyFor(anchorDay, period);
  if (endKey === null) {
    return [];
  }

  const byDate = new Map(rolled.map((point) => [point.date, point]));
  const count = ACTIVITY_BUCKET_COUNTS[period];
  return Array.from({ length: count }, (_, index) => {
    const offset = index - count + 1;
    const date = shiftBucketKey(endKey, period, offset);
    const source = byDate.get(date);
    return Object.fromEntries([
      ['date', date],
      ...ACTIVITY_FIELDS.map((field) => [
        field,
        toFiniteNumber(source?.[field]),
      ]),
    ]);
  });
}

export function activitySummary(points) {
  const series = Array.isArray(points) ? points : [];
  const totals = {
    totalRuns: 0,
    completed: 0,
    failed: 0,
    cancelled: 0,
    interrupted: 0,
  };
  let peak = null;
  for (const point of series) {
    const runs = toFiniteNumber(point?.runs);
    totals.totalRuns += runs;
    totals.completed += toFiniteNumber(point?.completed);
    totals.failed += toFiniteNumber(point?.failed);
    totals.cancelled += toFiniteNumber(point?.cancelled);
    totals.interrupted += toFiniteNumber(point?.interrupted);
    if (peak === null || runs > peak.runs) {
      peak = { date: point?.date ?? '', runs };
    }
  }
  return {
    ...totals,
    completionRate:
      totals.totalRuns > 0 ? totals.completed / totals.totalRuns : null,
    peak,
    scaleMax: niceScaleMax(peak?.runs ?? 0),
  };
}

export function formatActivityDate(
  dateKey,
  granularity,
  locale = 'en',
  { long = false } = {},
) {
  const date = bucketDate(dateKey);
  if (date === null) {
    return EM_DASH;
  }
  const options =
    granularity === 'month'
      ? long
        ? { month: 'long', year: 'numeric' }
        : { month: 'short' }
      : long
        ? { dateStyle: 'medium' }
        : { month: 'short', day: 'numeric' };
  return new Intl.DateTimeFormat(locale, {
    ...options,
    timeZone: 'UTC',
  }).format(date);
}

function bucketKeyFor(dateString, granularity) {
  if (typeof dateString !== 'string' || dateString.length < 7) {
    return null;
  }
  if (granularity === 'month') {
    return dateString.slice(0, 7);
  }
  const date = new Date(`${dateString}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  if (granularity === 'day') {
    return date.toISOString().slice(0, 10);
  }
  // week → the Monday of that ISO week, as a 'YYYY-MM-DD' string.
  const dayOfWeek = (date.getUTCDay() + 6) % 7; // Monday = 0
  date.setUTCDate(date.getUTCDate() - dayOfWeek);
  return date.toISOString().slice(0, 10);
}

function isoDayKey(isoString) {
  const date = parseIso(isoString);
  return date === null ? null : date.toISOString().slice(0, 10);
}

function lastSeriesDay(points) {
  const date = points.at(-1)?.date;
  if (typeof date !== 'string') {
    return null;
  }
  return date.length === 7 ? `${date}-01` : date;
}

function shiftBucketKey(dateKey, granularity, offset) {
  const date = bucketDate(dateKey);
  if (date === null) {
    return '';
  }
  if (granularity === 'month') {
    date.setUTCMonth(date.getUTCMonth() + offset);
    return date.toISOString().slice(0, 7);
  }
  date.setUTCDate(
    date.getUTCDate() + offset * (granularity === 'week' ? 7 : 1),
  );
  return date.toISOString().slice(0, 10);
}

function bucketDate(dateKey) {
  if (typeof dateKey !== 'string') {
    return null;
  }
  const normalized = dateKey.length === 7 ? `${dateKey}-01` : dateKey;
  const date = new Date(`${normalized}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function niceScaleMax(value) {
  const numeric = Math.max(0, toFiniteNumber(value));
  if (numeric === 0) {
    return 0;
  }
  const magnitude = 10 ** Math.floor(Math.log10(numeric));
  const normalized = numeric / magnitude;
  const ceiling =
    CHART_SCALE_STEPS.find((step) => normalized <= step) ??
    CHART_SCALE_STEPS.at(-1);
  return ceiling * magnitude;
}

// Build a `points="x,y …"` attribute for an SVG sparkline polyline. Values map
// left→right across `width`; the largest value touches the top of `height`.
// Pass `max` for an absolute scale (e.g. 1 for a 0–100% ratio series) instead
// of normalizing to the series' own maximum.
export function sparklinePoints(values, width, height, { max } = {}) {
  if (!Array.isArray(values) || values.length === 0) {
    return '';
  }
  const numeric = values.map(toFiniteNumber);
  if (numeric.length === 1) {
    const scale = max ?? numeric[0];
    return `0,${height} ${width},${height - barFraction(numeric[0], scale) * height}`;
  }
  const scale = max ?? Math.max(...numeric, 0);
  return numeric
    .map((value, index) => {
      const x = (index / (numeric.length - 1)) * width;
      const y = height - barFraction(value, scale) * height;
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

// Origin scopes that carry a detail after a colon (`agent:<id>`,
// `project:<name>`). `bundled` / `global` are bare scope tokens with no detail.
const SCOPED_ORIGIN_PREFIXES = Object.freeze(['agent', 'project']);

// Split a skill-usage origin string into `{ scope, detail }` for localized
// rendering. The report emits origins as short tokens: `bundled`, `global`,
// `agent:<id>`, `project:<name>`. `agent`/`project` carry a detail after the
// first colon (a project name may itself contain colons, so only the first is
// the separator); `bundled`/`global` (and any unknown/empty token) yield a null
// detail. The component maps `scope` to a localized word and shows `detail`
// verbatim (an agent id / project name), keeping this layer pure and testable.
export function parseOrigin(origin) {
  const raw = typeof origin === 'string' ? origin.trim() : '';
  if (!raw) {
    return { scope: '', detail: null };
  }
  const separatorIndex = raw.indexOf(':');
  if (separatorIndex === -1) {
    return { scope: raw, detail: null };
  }
  const scope = raw.slice(0, separatorIndex);
  const detail = raw.slice(separatorIndex + 1);
  if (!SCOPED_ORIGIN_PREFIXES.includes(scope) || detail.length === 0) {
    return { scope: raw, detail: null };
  }
  return { scope, detail };
}

// Format a Skill's evidence-backed offer conversion (matched activations / offers).
// The report sets `usage_rate` to null when `offered_sessions` is 0 (no
// opportunity to activate); that renders as an em dash, never `NaN` or a
// misleading `0%`. A present ratio renders as a whole percentage — a skill's
// activation rate needs no sub-percent precision to be read as a delete signal.
export function formatUsageRate(rate) {
  if (rate == null || !Number.isFinite(rate)) {
    return EM_DASH;
  }
  return formatPercent(rate, { fractionDigits: 0 });
}

// Roll the per-skill `by_agent` activation lists up into one agent→count list
// for the panel-wide "activations per agent" breakdown, summing an agent's
// activations across every skill. Keys are agent display keys (`agent@projekt`
// for project agents) already carried by the report. Returns entries sorted by
// count descending, then key ascending for a stable order among ties.
export function rollupSkillActivationsByAgent(skills) {
  const rows = Array.isArray(skills) ? skills : [];
  const byAgent = new Map();
  for (const skill of rows) {
    const entries = Array.isArray(skill?.by_agent) ? skill.by_agent : [];
    for (const entry of entries) {
      const key = typeof entry?.key === 'string' ? entry.key : '';
      if (!key) {
        continue;
      }
      byAgent.set(
        key,
        toFiniteNumber(byAgent.get(key)) + toFiniteNumber(entry?.count),
      );
    }
  }
  return [...byAgent.entries()]
    .map(([key, count]) => ({ key, count }))
    .sort((left, right) =>
      right.count !== left.count
        ? right.count - left.count
        : left.key < right.key
          ? -1
          : left.key > right.key
            ? 1
            : 0,
    );
}
import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
