// Live provider usage (provider.usage) for every Statistics view that shows
// it: the Overview's limits strip and the Limits sub-view. One shared answer
// and one poll serve them all; a view holds a watch while it is shown, each
// new watch revalidates at once, and the poll runs every ten seconds while
// any watch is held and the page is visible. Requests never overlap, and
// polling goes through the server's shared provider cache, so several views
// or windows do not multiply outbound provider requests.
import { getProviderUsage } from './api.js';
import { t } from './i18n.js';

const REFRESH_INTERVAL_MS = 10_000;

/**
 * The live usage: `report` is the last answer (null before the first and
 * after a failure), `error` the failure text of the newest request, and
 * `loading` is true while a request is in flight.
 */
export const providerUsage = $state({
  report: null,
  error: '',
  loading: false,
});

let request = null;
let generation = 0;
let watchers = 0;
let timer = null;

/** Loads usage now; a request already in flight is shared, not repeated. */
export function loadProviderUsage() {
  if (request) return request;
  const current = generation;
  providerUsage.loading = true;
  providerUsage.error = '';
  request = getProviderUsage()
    .then(
      (report) => {
        if (current !== generation) return;
        providerUsage.report = report ?? null;
      },
      (error) => {
        if (current !== generation) return;
        providerUsage.report = null;
        providerUsage.error = errorText(error);
      },
    )
    .finally(() => {
      if (current !== generation) return;
      providerUsage.loading = false;
      request = null;
    });
  return request;
}

/**
 * Holds a watch while a view shows live usage: it loads at once (joining a
 * request in flight) and keeps the poll running. Returns the release
 * function.
 */
export function watchProviderUsage() {
  watchers += 1;
  if (watchers === 1) {
    document.addEventListener('visibilitychange', handleVisibilityChange);
  }
  poll();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    watchers -= 1;
    if (watchers > 0) return;
    document.removeEventListener('visibilitychange', handleVisibilityChange);
    clearTimeout(timer);
    timer = null;
  };
}

/** Forgets the shared answer and any pending request (tests start empty). */
export function clearProviderUsage() {
  generation += 1;
  request = null;
  providerUsage.report = null;
  providerUsage.error = '';
  providerUsage.loading = false;
}

function pageHidden() {
  return document.visibilityState === 'hidden';
}

function poll() {
  if (pageHidden()) return;
  loadProviderUsage().then(schedule);
}

// One timer at most: each settled poll replaces the previous schedule.
function schedule() {
  clearTimeout(timer);
  timer = null;
  if (watchers === 0 || pageHidden()) return;
  timer = setTimeout(poll, REFRESH_INTERVAL_MS);
}

function handleVisibilityChange() {
  if (pageHidden()) {
    clearTimeout(timer);
    timer = null;
  } else {
    poll();
  }
}

function errorText(error) {
  if (typeof error?.message === 'string' && error.message.trim()) {
    return error.message.trim();
  }
  return t('statistics.limits.loadError');
}
