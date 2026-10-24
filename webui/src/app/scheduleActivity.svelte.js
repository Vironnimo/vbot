import { onDestroy, untrack } from 'svelte';
import { loadCronActivity } from '$lib/api.js';
import { createCoalescedRefresh } from '$lib/sessionInvalidation.js';

// The shell owns the counter even while Schedules is closed. The same snapshot
// feeds its rows; it contains counts and read cursors, never transcripts.
export function createScheduleActivity(context) {
  let jobs = $state({});
  let count = $state(0);
  let error = $state('');
  let disposed = false;
  let retryTimer;
  const refresher = createCoalescedRefresh(async () => {
    if (context.unavailable) return false;
    try {
      const response = await loadCronActivity();
      if (disposed) return false;
      jobs = response.jobs;
      count = response.notification_count;
      error = '';
      clearTimeout(retryTimer);
      return true;
    } catch (failure) {
      if (!disposed) {
        error = failure.message;
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => refresher.schedule(['schedules']), 10000);
      }
      return false;
    }
  });
  $effect(() => {
    void context.refreshToken;
    void context.sessionInvalidations;
    void context.sessionActivity;
    void context.connection;
    if (context.unavailable) return;
    untrack(() => refresher.schedule(['schedules']));
  });
  onDestroy(() => {
    disposed = true;
    clearTimeout(retryTimer);
    refresher.destroy();
  });
  return {
    get jobs() {
      return jobs;
    },
    get count() {
      return count;
    },
    get error() {
      return error;
    },
    refresh: () => refresher.run(['schedules']),
  };
}
