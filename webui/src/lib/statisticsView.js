// Pure display/formatting helpers for the Statistics tab. All non-trivial
// presentation logic lives here so the Svelte component stays display-only and
// this layer can be unit-tested in isolation. Locale-aware number formatting
// takes the active UI locale (`activeLocaleTag()` from i18n.js) so dates and
// numbers follow the app language, never the implicit browser locale.

import { parseAgentAddress } from './agentAddress.js';
import { t, tOr } from './i18n.js';
import { formatAbsoluteTime, formatMoment } from './timeText.js';

export const STATISTICS_SUB_VIEWS = Object.freeze([
  'overview',
  'usage',
  'compactions',
  'limits',
  'runs',
  'tools',
  'skills',
  'extensions',
]);

export const DAILY_GRANULARITIES = Object.freeze(['day', 'week', 'month']);
export const STATISTICS_RANGES = Object.freeze(['7d', '30d', '90d', 'all']);
export const USAGE_HISTORY_RANGES = Object.freeze(['24h', '7d', '30d', 'all']);

const ACTIVITY_BUCKET_COUNTS = Object.freeze({
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

export function modelCallStatusLabel(status) {
  return MODEL_CALL_STATUSES.has(status)
    ? t(`statistics.requestStatus.${status}`)
    : t('statistics.requestStatus.unknown');
}

function toFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

export function formatInteger(value, locale = 'en') {
  return new Intl.NumberFormat(locale).format(
    Math.round(toFiniteNumber(value)),
  );
}

export function formatChartTick(
  value,
  locale = 'en',
  { compact = false } = {},
) {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: 1,
    notation: compact ? 'compact' : 'standard',
  }).format(toFiniteNumber(value));
}

// Tokens are plain grouped integers today; kept distinct from formatInteger so a
// future compact form (1.2k) only has to change here.
export function formatTokens(value, locale = 'en') {
  return formatInteger(value, locale);
}

export function formatOptionalTokens(value, locale = 'en') {
  return value == null ? EM_DASH : formatTokens(value, locale);
}

export function formatCost(value, locale = 'en', { exact = false } = {}) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0)
    return EM_DASH;
  const tiny = !exact && value > 0 && value < 0.0001;
  return (
    (tiny ? '<' : '') +
    new Intl.NumberFormat(locale, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: exact ? 10 : value < 1 && value > 0 ? 4 : 2,
    }).format(tiny ? 0.0001 : value)
  );
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

function providerUsageTargetKey(snapshot) {
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

// How one snapshot relates to the one before it in the same series: a gap
// (no snapshot for too long), a reset (the window restarted or the reported
// reset time moved, so the values are not comparable), or a comparable change.
function usageHistoryStepKind(previous, current) {
  if (current.timestamp - previous.timestamp > USAGE_HISTORY_GAP_MS) {
    return 'gap';
  }
  const resetChanged =
    previous.resetAt !== current.resetAt &&
    (previous.resetAt !== null || current.resetAt !== null);
  return resetChanged || current.usedPercent < previous.usedPercent
    ? 'reset'
    : 'change';
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
      const kind = usageHistoryStepKind(previous, current);
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
    if (
      usageHistoryStepKind(safePoints[index - 1], safePoints[index]) !==
      'change'
    ) {
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

// Report rows attribute Extension-owned Sessions (for example Swarm
// participants) to `extension:<name>`; Agent ids never contain `:`.
const EXTENSION_ACTOR_PREFIX = 'extension:';

// Split a statistics `agent_id` into display parts. The `statistics.report`
// keys project agents as `agent@projekt` (and identity agents as a bare id), so
// every agent cell parses the address once and renders the bare name plus, for a
// project agent, a small project badge — instead of the raw `builder@vbot`
// string. An identity agent (no `@`) gets `projectId: null`, so the component
// renders it exactly as before (no badge), keeping the identity display
// byte-identical. An Extension key renders its Extension name with a badge.
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

// Short, stable suffix identifying an untitled Extension group in its label.
export function shortGroupId(groupId) {
  const value = typeof groupId === 'string' ? groupId : '';
  return value.slice(-6);
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
function rollupDaily(points, granularity) {
  if (granularity === 'day') {
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
  window = {},
) {
  const period = DAILY_GRANULARITIES.includes(granularity)
    ? granularity
    : 'day';
  const rolled = rollupDaily(points, period);
  const anchorDay =
    isoDayKey(window?.until ?? anchorIso) ?? lastSeriesDay(rolled);
  const endKey = bucketKeyFor(anchorDay, period);
  if (endKey === null) {
    return [];
  }

  const byDate = new Map(rolled.map((point) => [point.date, point]));
  const startKey = bucketKeyFor(isoDayKey(window?.since), period);
  const count = startKey
    ? period === 'month'
      ? (bucketDate(endKey).getUTCFullYear() -
          bucketDate(startKey).getUTCFullYear()) *
          12 +
        bucketDate(endKey).getUTCMonth() -
        bucketDate(startKey).getUTCMonth() +
        1
      : Math.round(
          (bucketDate(endKey) - bucketDate(startKey)) /
            (DAY_MS * (period === 'week' ? 7 : 1)),
        ) + 1
    : ACTIVITY_BUCKET_COUNTS[period];
  const fields = new Set(ACTIVITY_FIELDS);
  for (const point of rolled) {
    for (const [key, value] of Object.entries(point)) {
      if (typeof value === 'number') fields.add(key);
    }
  }
  return Array.from({ length: Math.max(0, count) }, (_, index) => {
    const offset = index - count + 1;
    const date = shiftBucketKey(endKey, period, offset);
    const source = byDate.get(date);
    return Object.fromEntries([
      ['date', date],
      ...[...fields].map((field) => [field, toFiniteNumber(source?.[field])]),
    ]);
  });
}

// UTC calendar windows match the report's daily aggregation boundary. The last
// day is partial; an explicit until keeps every section on the same snapshot.
export function statisticsWindow(range, now = Date.now()) {
  if (range === 'all' || !STATISTICS_RANGES.includes(range)) return {};
  const until = new Date(now);
  const since = new Date(until);
  since.setUTCHours(0, 0, 0, 0);
  since.setUTCDate(since.getUTCDate() - Number.parseInt(range, 10) + 1);
  return { since: since.toISOString(), until: until.toISOString() };
}

export function timelineTicks(points) {
  if (!points.length) return [];
  return [
    ...new Set([
      points[0],
      points[Math.floor((points.length - 1) / 2)],
      points.at(-1),
    ]),
  ];
}

export function tokenTimeline(points) {
  const series = points.map((point) => ({ ...point, ...tokenSplit(point) }));
  return {
    points: series,
    scaleMax: niceScaleMax(Math.max(0, ...series.map((point) => point.total))),
  };
}

export function statisticsInsights(report) {
  const overview = report?.overview;
  const tools = report?.tools;
  const totalRuns = toFiniteNumber(overview?.total_runs);
  const rows = tools?.tools ?? [];
  const accepted = rows.reduce(
    (sum, tool) => sum + toFiniteNumber(tool.successes),
    0,
  );
  const rejected = rows.reduce(
    (sum, tool) => sum + toFiniteNumber(tool.failures),
    0,
  );
  const codes = new Map();
  for (const tool of rows) {
    for (const entry of tool.error_codes ?? []) {
      codes.set(entry.key, (codes.get(entry.key) ?? 0) + entry.count);
    }
  }
  return {
    activeDays: (overview?.daily_trend ?? []).filter((point) => point.runs > 0)
      .length,
    toolRunShare:
      totalRuns > 0
        ? toFiniteNumber(overview.runs_with_tool_calls) / totalRuns
        : null,
    accepted,
    rejected,
    unknown: Math.max(
      0,
      toFiniteNumber(tools?.total_calls) - accepted - rejected,
    ),
    rejectionCodes: [...codes]
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
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

function barFraction(value, max) {
  return max > 0 ? Math.max(0, value) / max : 0;
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

// ---------------------------------------------------------------------------
// Tooltip content. Each builder returns what the shared `use:tooltip` action
// accepts (lib/tooltip.js): '' for nothing to add, a string, or
// `{ title, text, rows }`. Definitions live in InfoHints on labels and column
// headers; these carry the facts about one value (design.md, "Tooltip
// content").

const USED_PERCENT_FORMAT = { maximumFractionDigits: 1 };

function formatUsedPercent(value, locale) {
  return `${new Intl.NumberFormat(locale, USED_PERCENT_FORMAT).format(
    clampUsagePercent(value),
  )}%`;
}

function tokenBreakdownRows(record, locale) {
  const rows = [
    {
      label: t('statistics.tokens.measuredInput'),
      value: formatTokens(record?.measured_input_tokens, locale),
    },
    {
      label: t('statistics.tokens.measuredOutput'),
      value: formatTokens(record?.measured_output_tokens, locale),
    },
  ];
  if (toFiniteNumber(record?.estimated_input_tokens) > 0) {
    rows.push({
      label: t('statistics.tokens.estimatedInput'),
      value: formatTokens(record.estimated_input_tokens, locale),
      tone: 'warning',
    });
  }
  if (toFiniteNumber(record?.estimated_output_tokens) > 0) {
    rows.push({
      label: t('statistics.tokens.estimatedOutput'),
      value: formatTokens(record.estimated_output_tokens, locale),
      tone: 'warning',
    });
  }
  if (toFiniteNumber(record?.reasoning_turns) > 0) {
    rows.push({
      label: t('statistics.tokens.reasoning'),
      value: t('statistics.tokens.reasoningValue', {
        count: formatTokens(record.reasoning_tokens, locale),
      }),
    });
  }
  const hitRate = cacheHitRate(record);
  if (hitRate !== null) {
    rows.push({
      label: t('statistics.col.cacheRead'),
      value: t('statistics.tokens.cacheValue', {
        count: formatTokens(record.cache_read_tokens, locale),
        rate: formatPercent(hitRate),
      }),
    });
  }
  return rows;
}

/** A token total with its measured/estimated input/output breakdown. */
export function tokenBreakdownTooltip(record, locale = 'en') {
  return {
    title: t('statistics.tokens.total', {
      count: formatTokens(tokenSplit(record).total, locale),
    }),
    rows: tokenBreakdownRows(record, locale),
  };
}

/** One token-trend period: total, breakdown, and the period's Runs. */
export function tokenPeriodTooltip(point, periodLabel, locale = 'en') {
  const split = tokenSplit(point);
  const activity = [];
  if (point?.runs != null) {
    activity.push({
      label: t('statistics.col.runs'),
      value: formatInteger(point.runs, locale),
    });
  }
  if (toFiniteNumber(point?.errors) > 0) {
    activity.push({
      label: t('statistics.col.errors'),
      value: formatInteger(point.errors, locale),
      tone: 'danger',
    });
  }
  if (split.total === 0) {
    return {
      title: periodLabel,
      text: t('statistics.tokens.nonePeriod'),
      rows: activity,
    };
  }
  return {
    title: periodLabel,
    rows: [
      {
        label: t('statistics.tokens.totalLabel'),
        value: formatTokens(split.total, locale),
      },
      ...tokenBreakdownRows(point, locale),
      ...activity,
    ],
  };
}

/** Hour span in UTC, such as "09:00–10:00 UTC". */
export function formatHourRange(hour) {
  const start = Math.max(0, Math.min(23, Math.round(toFiniteNumber(hour))));
  return t('statistics.errors.hourRange', {
    from: formatHourLabel(start),
    to: formatHourLabel((start + 1) % 24),
  });
}

/** One errors-by-hour column: its count and share of all errors. */
export function errorHourTooltip(entry, totalErrors, locale = 'en') {
  const count = toFiniteNumber(entry?.count);
  const total = toFiniteNumber(totalErrors);
  return {
    title: formatHourRange(entry?.hour),
    rows: [
      {
        label: t('statistics.col.errors'),
        value: formatInteger(count, locale),
        tone: count > 0 ? 'danger' : '',
      },
      {
        label: t('statistics.col.share'),
        value:
          total > 0
            ? t('statistics.errors.shareOfTotal', {
                share: formatShare(count, total),
                total: formatInteger(total, locale),
              })
            : '',
      },
    ],
  };
}

/** A Run-status share such as "4 of 125 Runs". */
export function runShareDetail(count, total, locale = 'en') {
  return t('statistics.runs.ofRuns', {
    count: formatInteger(count, locale),
    total: formatInteger(total, locale),
  });
}

/** Failed, cancelled and interrupted Runs behind a "not completed" count. */
export function unfinishedRunsTooltip(runStatus, locale = 'en') {
  return {
    rows: ['failed', 'cancelled', 'interrupted'].map((status) => ({
      label: tOr(`statistics.status.${status}`, status),
      value: formatInteger(runStatus?.[status], locale),
      tone:
        status === 'failed' && toFiniteNumber(runStatus?.[status]) > 0
          ? 'danger'
          : '',
    })),
  };
}

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

/**
 * The basis of one cost cell: how many of the row's calls it covers, the
 * exact amount when the cell rounds it, and how many estimates use today's
 * catalog. `kind` is 'reported', 'estimated' or 'unpriced'.
 */
export function costCellTooltip(totals, kind, locale = 'en') {
  const calls = toFiniteNumber(totals?.calls);
  const share = (count) =>
    t('statistics.cost.callsOf', {
      count: formatInteger(count, locale),
      total: formatInteger(calls, locale),
    });
  if (kind === 'unpriced') {
    const count = toFiniteNumber(totals?.unpriced_calls);
    return count > 0
      ? t('statistics.cost.unpricedDetail', { calls: share(count) })
      : '';
  }
  const reported = kind === 'reported';
  const count = toFiniteNumber(
    reported ? totals?.reported_calls : totals?.estimated_calls,
  );
  if (count <= 0) {
    return '';
  }
  const amount = reported ? totals?.reported_usd : totals?.estimated_usd;
  const exact = formatCost(amount, locale, { exact: true });
  const rows = [
    { label: t('statistics.cost.callsShort'), value: share(count) },
  ];
  if (exact !== formatCost(amount, locale)) {
    rows.push({ label: t('statistics.cost.exact'), value: exact });
  }
  if (!reported && toFiniteNumber(totals?.retrospective_calls) > 0) {
    rows.push({
      label: t('statistics.cost.retrospective'),
      value: formatInteger(totals.retrospective_calls, locale),
    });
  }
  return { rows };
}

/** A Tool's rejection codes with their counts, most frequent first. */
export function toolRejectionTooltip(tool, locale = 'en') {
  const codes = Array.isArray(tool?.error_codes) ? tool.error_codes : [];
  if (codes.length === 0) {
    return '';
  }
  return {
    title: t('statistics.tools.rejectionsByCode', {
      count: formatInteger(tool.failures, locale),
    }),
    rows: codes.map((entry) => ({
      label: formatInteger(entry.count, locale),
      value: entry.key,
      mono: true,
    })),
  };
}

/** The evidence behind a Skill's offer conversion. */
export function skillConversionTooltip(skill, locale = 'en') {
  const offered = toFiniteNumber(skill?.offered_sessions);
  if (offered === 0) {
    return t('statistics.skills.noOfferDataRowTitle');
  }
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
  if (!known) {
    return '';
  }
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

// Evenly spaced point indices, always keeping the first and the last.
function sampledIndices(length, maxCount) {
  if (length <= maxCount) {
    return Array.from({ length }, (_, index) => index);
  }
  const indices = new Set();
  for (let step = 0; step < maxCount; step += 1) {
    indices.add(Math.round((step * (length - 1)) / (maxCount - 1)));
  }
  return [...indices].sort((left, right) => left - right);
}

/**
 * Hover and focus slots over a usage-history trace: each covers the span
 * nearest to one point (at most `maxSlots` evenly chosen points), in percent
 * of the plot, with the point's own position inside the slot.
 */
export function usageHistorySlots(
  points,
  maxSlots = 96,
  width = 720,
  height = 160,
) {
  const safePoints = Array.isArray(points) ? points : [];
  if (safePoints.length === 0) {
    return [];
  }
  const coordinates = usageHistoryPointCoordinates(safePoints, width, height);
  const indices = sampledIndices(safePoints.length, Math.max(2, maxSlots));
  return indices.map((index, position) => {
    const x = coordinates[index].x;
    const start =
      position === 0 ? 0 : (coordinates[indices[position - 1]].x + x) / 2;
    const end =
      position === indices.length - 1
        ? width
        : (coordinates[indices[position + 1]].x + x) / 2;
    const span = Math.max(0, end - start);
    return {
      index,
      left: (start / width) * 100,
      width: (span / width) * 100,
      at: span > 0 ? ((x - start) / span) * 100 : 50,
      y: (coordinates[index].y / height) * 100,
    };
  });
}

function usageStepRow(previous, current, locale) {
  const kind = usageHistoryStepKind(previous, current);
  const values = `${formatUsedPercent(previous.usedPercent, locale)} → ${formatUsedPercent(current.usedPercent, locale)}`;
  const label = t('statistics.limits.sincePrevious');
  if (kind === 'gap') {
    return {
      label,
      value: t('statistics.limits.gapDetail', {
        duration: formatRelativeDuration(
          current.timestamp - previous.timestamp,
        ),
      }),
      tone: 'muted',
    };
  }
  if (kind === 'reset') {
    return { label, value: t('statistics.limits.resetDetail', { values }) };
  }
  return {
    label,
    value: `${formatUsageDelta(current.usedPercent - previous.usedPercent, locale)} (${values})`,
  };
}

/** One snapshot of a usage-history trace. */
export function usageHistoryPointTooltip(
  points,
  index,
  locale = 'en',
  nowMs = Date.now(),
) {
  const point = points?.[index];
  if (!point) {
    return '';
  }
  const rows = [
    {
      label: t('statistics.limits.used'),
      value: formatUsedPercent(point.usedPercent, locale),
    },
  ];
  if (index > 0) {
    rows.push(usageStepRow(points[index - 1], point, locale));
  }
  if (point.resetAt) {
    rows.push({
      label: t('statistics.limits.resets'),
      value: formatMoment(point.resetAt, { nowMs }),
    });
  }
  return { title: formatMoment(point.sampledAt, { nowMs }), rows };
}

/** One entry of the largest-changes list: both snapshots and what changed. */
export function usageHistoryIntervalTooltip(
  interval,
  locale = 'en',
  nowMs = Date.now(),
) {
  if (!interval) {
    return '';
  }
  return {
    title: `${interval.displayName} · ${interval.label}`,
    rows: [
      usageStepRow(interval.from, interval.to, locale),
      {
        label: t('statistics.limits.from'),
        value: formatAbsoluteTime(interval.from.sampledAt),
      },
      {
        label: t('statistics.limits.to'),
        value: formatMoment(interval.to.sampledAt, { nowMs }),
      },
      { label: t('statistics.limits.account'), value: interval.account },
    ],
  };
}

import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
