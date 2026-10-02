// The Statistics view's report cache. Each Statistics tab requests only its
// own report sections; the last answer per (sections, range, time zone) stays
// here for the lifetime of the page, so returning to a tab or range shows
// its numbers at once while a fresh request revalidates them in the
// background (stale-while-revalidate). Requests for different keys run
// independently; for one key only the newest request may settle its entry,
// so a slow older answer never overwrites a newer one.
import { SvelteMap } from 'svelte/reactivity';

import { getStatisticsReport } from './api.js';

const EMPTY_ENTRY = Object.freeze({
  report: null,
  error: null,
  loading: false,
});

const entries = new SvelteMap();
// The newest request token per key; not reactive state.
// eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
const latestRequest = new Map();
let nextToken = 0;

/**
 * The cached entry of a report key: `{ report, error, loading }`. `report`
 * is the last successful answer (null before the first), `error` the
 * failure of the newest request (null after a success), and `loading` is
 * true while a request is in flight.
 */
export function statisticsReport(key) {
  return entries.get(key) ?? EMPTY_ENTRY;
}

/**
 * Requests the report of `key` with `params`. Without `force`, a key that is
 * already loading keeps its pending request; `force` starts a new request
 * that supersedes it. Resolves once the request settles.
 */
export function requestStatisticsReport(key, params, { force = false } = {}) {
  const current = entries.get(key) ?? EMPTY_ENTRY;
  if (current.loading && !force) return Promise.resolve();
  nextToken += 1;
  const token = nextToken;
  latestRequest.set(key, token);
  entries.set(key, { report: current.report, error: null, loading: true });
  return getStatisticsReport(params).then(
    (report) => {
      if (latestRequest.get(key) !== token) return;
      entries.set(key, { report, error: null, loading: false });
    },
    (error) => {
      if (latestRequest.get(key) !== token) return;
      const settled = entries.get(key) ?? EMPTY_ENTRY;
      entries.set(key, {
        report: settled.report,
        error: error ?? new Error(''),
        loading: false,
      });
    },
  );
}

/** Forgets every cached report (tests start from an empty cache). */
export function clearStatisticsReports() {
  entries.clear();
  latestRequest.clear();
}
