// Pure helpers of the Statistics Limits tab: live Subscription usage windows
// (provider.usage) and their hourly history (provider.usage_history), with the
// vBot Runs that overlap a history interval (statistics.run_activity). The
// local Statistics report sections live in statisticsView.js.

import { t } from './i18n.js';
import { formatDateTime } from './statisticsView.js';
import { formatAbsoluteTime, formatMoment } from './timeText.js';

export const USAGE_HISTORY_RANGES = Object.freeze(['24h', '7d', '30d', 'all']);

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

/** A Run's input and output tokens, reported and estimated alike. */
export function runTokens(run) {
  return (
    toFiniteNumber(run?.measured_input_tokens) +
    toFiniteNumber(run?.measured_output_tokens) +
    toFiniteNumber(run?.estimated_input_tokens) +
    toFiniteNumber(run?.estimated_output_tokens)
  );
}

export function runActivityTotals(runs) {
  const totals = { runs: 0, tokens: 0 };
  for (const run of Array.isArray(runs) ? runs : []) {
    totals.runs += 1;
    totals.tokens += runTokens(run);
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

const USED_PERCENT_FORMAT = { maximumFractionDigits: 1 };

function formatUsedPercent(value, locale) {
  return `${new Intl.NumberFormat(locale, USED_PERCENT_FORMAT).format(
    clampUsagePercent(value),
  )}%`;
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
