<script>
  import { onDestroy, untrack } from 'svelte';
  import { listCronRuns, markCronRead } from '$lib/api.js';
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/timeText.js';
  import { createCoalescedRefresh } from '$lib/sessionInvalidation.js';
  import { t } from '$lib/i18n.js';
  import { runStatusLabel } from '$lib/statisticsView.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';

  let { jobId, activity = {}, onOpenSession, onRead = () => {} } = $props();
  let rows = $state([]);
  let nextBefore = $state(null);
  let wanted = 5;
  let loading = $state(true);
  let reading = $state(false);
  let error = $state('');
  let disposed = false;
  let initialized = false;
  const refresh = createCoalescedRefresh(async () => {
    loading = true;
    try {
      const count = wanted;
      let before = null;
      const loaded = [];
      do {
        const page = await listCronRuns(jobId, {
          before,
          limit: Math.min(50, count - loaded.length),
        });
        loaded.push(...page.runs);
        before = page.next_before;
      } while (!disposed && before && loaded.length < count);
      if (disposed) return;
      rows = loaded;
      nextBefore = before;
      error = '';
    } catch (failure) {
      if (!disposed) error = failure.message;
    } finally {
      if (!disposed) loading = false;
    }
  });
  $effect(() => {
    void activity;
    untrack(() => {
      if (initialized) refresh.schedule();
      else {
        initialized = true;
        void refresh.run();
      }
    });
  });
  onDestroy(() => {
    disposed = true;
    refresh.destroy();
  });

  function more() {
    wanted += 5;
    void refresh.run();
  }
  async function markRead() {
    reading = true;
    try {
      await markCronRead(jobId, activity);
      await Promise.all([refresh.run(), onRead()]);
    } catch (failure) {
      error = failure.message;
    } finally {
      reading = false;
    }
  }
</script>

<div class="cron-runs">
  {#if error}
    <Banner variant="error">{error}</Banner>
    <Button variant="tertiary" onClick={() => refresh.run()}
      >{t('common.retry')}</Button
    >
  {/if}
  {#if activity.unread_count > 0 || activity.alert_at}
    <Button variant="tertiary" disabled={reading} onClick={markRead}
      >{t('cron.runs.read')}</Button
    >
  {/if}
  <ul aria-busy={loading}>
    {#each rows as run (run.run_id)}
      <li>
        <button
          class="cron-run"
          type="button"
          onclick={() =>
            onOpenSession(
              formatAgentAddress(run.agent_id, run.project_id),
              run.session_id,
              false,
              run.run_id,
            )}
        >
          <span class="cron-run-title">{run.title}</span>
          {#if run.unread}<span
              class="unread-dot"
              role="img"
              aria-label={t('notifications.unread')}
            ></span>{/if}
          <span class="cron-run-meta">
            <time
              datetime={run.started_at}
              title={formatAbsoluteTime(run.started_at)}
              >{formatRelativeTime(run.started_at)}</time
            >
            <span>{runStatusLabel(run.status)}</span>
          </span>
        </button>
      </li>
    {/each}
  </ul>
  {#if rows.length === 0 && !error}
    <p role="status">
      {loading ? t('cron.runs.loading') : t('cron.runs.empty')}
    </p>
  {/if}
  {#if nextBefore}
    <Button variant="tertiary" disabled={loading} onClick={more}
      >{t('cron.runs.more')}</Button
    >
  {/if}
</div>

<style>
  .cron-runs {
    padding: 4px 8px 12px 24px;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 4px 0;
  }
  .cron-run {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 5px;
    text-align: left;
    padding: 9px 8px;
    border: 0;
    border-radius: 6px;
    color: var(--text-hi);
    background: transparent;
    cursor: pointer;
  }
  .cron-run:hover,
  .cron-run:focus-visible {
    background: var(--surface-3);
  }
  .cron-run-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .cron-run-meta {
    grid-column: 1 / -1;
    display: flex;
    justify-content: space-between;
    gap: 8px;
    color: var(--text-lo);
    font-size: 11px;
  }
  .unread-dot {
    width: 7px;
    height: 7px;
    background: var(--blue);
    border-radius: 50%;
    align-self: center;
  }
  p {
    margin: 8px;
    color: var(--text-lo);
    font-size: 12px;
  }
</style>
