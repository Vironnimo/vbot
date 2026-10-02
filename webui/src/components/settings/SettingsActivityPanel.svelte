<script>
  import Button from '../ui/Button.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';
  import { cancelLocalSetup, dismissBackgroundActivity } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { describeBackgroundActivity } from '$lib/settingsView.js';

  const noop = () => {};

  // `activities` is the server's background activity list, kept current by
  // its `activity_status` pushes; `onOpen` shows the Settings section that
  // manages an entry's work.
  let { activities = [], onOpen = noop, onError = noop } = $props();

  let rows = $derived(activities.map(describeBackgroundActivity));
  // Entries whose cancel or dismiss request is in flight. The next push
  // shows the result.
  let pending = $state([]);

  async function act(id, request) {
    if (pending.includes(id)) return;
    pending = [...pending, id];
    try {
      await request();
    } catch (error) {
      onError(error?.message || t('settings.activity.actionFailed'));
    } finally {
      pending = pending.filter((pendingId) => pendingId !== id);
    }
  }
</script>

{#if rows.length === 0}
  <p class="s-row-desc activity-empty">{t('settings.activity.empty')}</p>
{:else}
  <ul class="s-group activity-list">
    {#each rows as row (row.id)}
      <li class="s-row s-row--stacked activity-row" data-activity={row.id}>
        <div class="activity-head">
          <div class="s-row-info">
            <div class="s-row-label">{row.title}</div>
            <div class="s-row-desc" class:activity-warn={row.warn}>
              {row.status}
            </div>
          </div>
          <div class="activity-actions">
            {#if row.section}
              <Button variant="tertiary" onClick={() => onOpen(row.section)}>
                {t('settings.activity.open')}
              </Button>
            {/if}
            {#if row.cancelTarget}
              <Button
                loading={pending.includes(row.id)}
                onClick={() =>
                  act(row.id, () => cancelLocalSetup(row.cancelTarget))}
              >
                {t('settings.activity.cancel')}
              </Button>
            {/if}
            {#if row.dismissible}
              <Button
                variant="tertiary"
                loading={pending.includes(row.id)}
                onClick={() =>
                  act(row.id, () => dismissBackgroundActivity(row.id))}
              >
                {t('settings.activity.dismiss')}
              </Button>
            {/if}
          </div>
        </div>
        {#if row.progress}
          <ProgressBar
            label={t('settings.activity.progressLabel', { title: row.title })}
            percent={row.progress.percent}
            text={row.progress.text}
          />
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .activity-empty {
    margin: 0;
  }

  .activity-list {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .activity-row {
    gap: 8px;
  }

  .activity-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 16px;
  }

  .activity-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .activity-warn {
    color: var(--amber);
  }
</style>
