// Lean always-on browser measurements, batched into the server's performance
// sink about once a minute.
//
// The server already times every RPC it serves and counts the events it
// publishes. The browser adds what only it can see: how long RPCs take
// including its own connection queue, which invalidation started a wave of
// RPCs, main-thread Long Tasks, and how often the Extension host invalidated
// an open page. Names and buckets follow the server's report contract
// (performance.md); the server skips names it does not know. Until
// `startClientMetrics` runs, every note is a no-op.

const REPORT_INTERVAL_MS = 60_000;
// RPCs starting this soon after an invalidation count as its first wave.
export const INVALIDATION_WAVE_MS = 250;
const MAX_REPORT_NAMES = 200;

// The server's histogram buckets: 10% steps from 0.01 ms up to one hour, with
// an underflow bucket 0 and an overflow bucket after the last step.
const BUCKET_FACTOR = 1.1;
const MIN_BUCKET_MS = 0.01;
const MAX_BUCKET_MS = 3_600_000;
const LOG_FACTOR = Math.log(BUCKET_FACTOR);
const BUCKET_COUNT = Math.ceil(
  Math.log(MAX_BUCKET_MS / MIN_BUCKET_MS) / LOG_FACTOR,
);

const LONG_TASK_ENTRY = 'longtask';
const LONG_ANIMATION_FRAME_ENTRY = 'long-animation-frame';

let active = null;

export function bucketIndex(ms) {
  if (!(ms >= MIN_BUCKET_MS)) return 0;
  if (ms >= MAX_BUCKET_MS) return BUCKET_COUNT + 1;
  return Math.min(
    Math.floor(Math.log(ms / MIN_BUCKET_MS) / LOG_FACTOR) + 1,
    BUCKET_COUNT,
  );
}

// Start measuring and reporting through `report(payload)`; returns the stop
// function. A failed report drops its batch: measurements stay bounded and
// best-effort.
export function startClientMetrics({
  report,
  intervalMs = REPORT_INTERVAL_MS,
  now = () => globalThis.performance.now(),
  Observer = globalThis.PerformanceObserver,
} = {}) {
  active?.stop();
  const collector = {
    now,
    histograms: new Map(),
    counters: new Map(),
    lastInvalidation: null,
    stop: null,
  };
  const observers = [
    observeEntries(Observer, LONG_TASK_ENTRY, 'webui.long_task', collector),
    observeEntries(
      Observer,
      LONG_ANIMATION_FRAME_ENTRY,
      'webui.long_animation_frame',
      collector,
    ),
  ].filter(Boolean);
  const timer = setInterval(() => flush(collector, report), intervalMs);
  collector.stop = () => {
    clearInterval(timer);
    for (const observer of observers) observer.disconnect();
    if (active === collector) active = null;
  };
  active = collector;
  return collector.stop;
}

// Note one RPC starting now; call the returned function with its outcome.
export function trackRpc(method) {
  const collector = active;
  if (!collector) return noop;
  const startedAt = collector.now();
  const wave = collector.lastInvalidation;
  if (wave && startedAt - wave.at <= INVALIDATION_WAVE_MS) {
    addCount(collector, `webui.invalidation_rpcs.${wave.kind}.${method}`);
  }
  return (ok) => {
    if (active !== collector) return;
    addDuration(collector, 'webui.rpc', collector.now() - startedAt);
    if (!ok) addCount(collector, 'webui.rpc_errors');
  };
}

// Note a `resource_changed` invalidation before its handlers run.
export function noteInvalidation(kind) {
  const collector = active;
  if (!collector || typeof kind !== 'string' || !kind) return;
  addCount(collector, `webui.invalidations.${kind}`);
  collector.lastInvalidation = { kind, at: collector.now() };
}

// Note that the Extension host told an open page to refresh its data.
export function noteExtensionPageInvalidation(reason) {
  const collector = active;
  if (!collector) return;
  addCount(collector, `webui.extension_page.invalidations.${reason}`);
}

function observeEntries(Observer, entryType, metric, collector) {
  if (!Observer?.supportedEntryTypes?.includes(entryType)) return null;
  const observer = new Observer((list) => {
    for (const entry of list.getEntries()) {
      addDuration(collector, metric, entry.duration);
    }
  });
  observer.observe({ type: entryType });
  return observer;
}

function hasRoom(collector, name, map) {
  return (
    map.has(name) ||
    collector.histograms.size + collector.counters.size < MAX_REPORT_NAMES
  );
}

function addCount(collector, name) {
  if (!hasRoom(collector, name, collector.counters)) return;
  collector.counters.set(name, (collector.counters.get(name) ?? 0) + 1);
}

function addDuration(collector, name, ms) {
  if (!hasRoom(collector, name, collector.histograms)) return;
  const value = Number.isFinite(ms) && ms > 0 ? ms : 0;
  let histogram = collector.histograms.get(name);
  if (!histogram) {
    histogram = { count: 0, sum: 0, min: value, max: value, buckets: {} };
    collector.histograms.set(name, histogram);
  }
  histogram.count += 1;
  histogram.sum += value;
  histogram.min = Math.min(histogram.min, value);
  histogram.max = Math.max(histogram.max, value);
  const index = bucketIndex(value);
  histogram.buckets[index] = (histogram.buckets[index] ?? 0) + 1;
}

function flush(collector, report) {
  if (collector.histograms.size === 0 && collector.counters.size === 0) return;
  const payload = {
    histograms: Object.fromEntries(
      [...collector.histograms].map(([name, histogram]) => [
        name,
        {
          count: histogram.count,
          sum_ms: histogram.sum,
          min_ms: histogram.min,
          max_ms: histogram.max,
          buckets: histogram.buckets,
        },
      ]),
    ),
    counters: Object.fromEntries(collector.counters),
  };
  collector.histograms = new Map();
  collector.counters = new Map();
  Promise.resolve()
    .then(() => report(payload))
    .catch(() => {});
}

function noop() {}
