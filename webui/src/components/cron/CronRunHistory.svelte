<script>
  import { onDestroy, untrack } from 'svelte';
  import { listCronRuns, markCronRead } from '$lib/api.js';
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import { formatAbsoluteTime, formatRelativeTime } from '$lib/timeText.js';
  import { createCoalescedRefresh } from '$lib/sessionInvalidation.js';
  import { t } from '$lib/i18n.js';
  import { runStatusLabel, runStatusVariant } from '$lib/statisticsView.js';
  import { tooltip } from '$lib/tooltip.js';
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
  <ul aria-busy={loading}>
    {#each rows as run (run.run_id)}
      <li>
        <button
          class="cron-run"
          type="button"
          use:tooltip={() => ({
            title: run.title,
            rows: [
              {
                label: t('cron.card.status'),
                value: runStatusLabel(run.status),
              },
              {
                label: t('cron.runs.started'),
                value: formatAbsoluteTime(run.started_at),
              },
            ],
          })}
          onclick={() =>
            onOpenSession(
              formatAgentAddress(run.agent_id, run.project_id),
              run.session_id,
            )}
        >
          <span class="cron-run-title">{run.title}</span>
          {#if run.unread}<span
              class="unread-dot"
              role="img"
              aria-label={t('notifications.unread')}
            ></span>{/if}
          <span class="cron-run-meta">
            {#if run.status !== 'completed'}
              <span
                class="cron-run-status cron-run-status--{runStatusVariant(
                  run.status,
                )}">{runStatusLabel(run.status)}</span
              >
            {/if}
            <time datetime={run.started_at}
              >{formatRelativeTime(run.started_at, Date.now(), {
                style: 'narrow',
              })}</time
            >
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
  {#if nextBefore || activity.unread_count > 0 || activity.alert_at}
    <div class="cron-runs-actions">
      {#if nextBefore}
        <button type="button" disabled={loading} onclick={more}
          >{t('cron.runs.more')}</button
        >
      {/if}
      {#if activity.unread_count > 0 || activity.alert_at}
        <button type="button" disabled={reading} onclick={markRead}
          >{t('cron.runs.read')}</button
        >
      {/if}
    </div>
  {/if}
</div>

<style>
  /* The history hangs under its Automation with its titles aligned to the
     Automation's name: one line per Run, its time at the end and its status
     only when the Run did not complete. */
  .cron-runs {
    padding: 0 0 4px 14px;
  }
  ul {
    display: flex;
    flex-direction: column;
    gap: 1px;
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .cron-run {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 6px;
    text-align: left;
    padding: 3px 10px;
    border: 0;
    border-radius: var(--r-md);
    color: var(--text-med);
    background: transparent;
    font-size: var(--fs-label-md);
    line-height: 1.35;
    cursor: pointer;
    transition: background 120ms ease;
  }
  .cron-run:hover,
  .cron-run:focus-visible {
    background: var(--surface-2);
    color: var(--text-hi);
  }
  .cron-run:focus-visible {
    box-shadow: inset 0 0 0 2px var(--accent);
    outline: none;
  }
  .cron-run-title {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .cron-run-meta {
    display: flex;
    flex-shrink: 0;
    gap: 6px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .cron-run-status--error {
    color: var(--red);
  }
  .cron-run-status--warn {
    color: var(--amber);
  }
  .cron-run-status--info {
    color: var(--blue);
  }
  .unread-dot {
    width: 6px;
    height: 6px;
    flex-shrink: 0;
    background: var(--blue);
    border-radius: 50%;
  }
  .cron-runs-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    padding: 3px 10px 2px;
  }
  .cron-runs-actions button {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-med);
    font: inherit;
    font-size: var(--fs-label-sm);
    cursor: pointer;
  }
  .cron-runs-actions button:hover:not(:disabled),
  .cron-runs-actions button:focus-visible {
    outline: none;
    color: var(--text-hi);
  }
  .cron-runs-actions button:disabled {
    cursor: default;
    opacity: 0.6;
  }
  p {
    margin: 0;
    padding: 3px 10px;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
</style>
