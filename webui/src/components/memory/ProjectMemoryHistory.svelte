<script>
  import { onDestroy, untrack } from 'svelte';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import { listProjectMemoryHistory, revertProjectMemory } from '$lib/api.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';

  let {
    projectId,
    refreshToken,
    disabled = false,
    onReverted,
    onBusyChange = () => {},
    onToast = () => {},
  } = $props();
  let open = $state(false);
  let revisions = $state([]);
  let loading = $state(false);
  let reverting = $state(false);
  let pendingRevision = $state(null);
  let error = $state('');
  let request = 0;
  let destroyed = false;

  async function load() {
    const current = ++request;
    loading = true;
    error = '';
    try {
      const result = await listProjectMemoryHistory(projectId);
      if (!destroyed && current === request) revisions = result.revisions ?? [];
    } catch (cause) {
      if (!destroyed && current === request)
        error = cause?.message || t('memory.history.loadError');
    } finally {
      if (!destroyed && current === request) loading = false;
    }
  }

  async function revert() {
    const revision = pendingRevision;
    if (revision === null || reverting || disabled) return;
    pendingRevision = null;
    reverting = true;
    onBusyChange(true);
    error = '';
    request += 1;
    try {
      const result = await revertProjectMemory(projectId, revision);
      if (destroyed) return;
      onReverted(result);
      onToast(t('memory.history.reverted'));
      await load();
    } catch (cause) {
      if (!destroyed) error = cause?.message || t('memory.history.revertError');
    } finally {
      if (!destroyed) {
        reverting = false;
        onBusyChange(false);
      }
    }
  }

  function dateLabel(value) {
    return formatDateTimeInApplicationZone(value, activeLocaleTag(), {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
  }

  $effect(() => {
    refreshToken;
    if (open) untrack(() => void load());
  });

  onDestroy(() => {
    destroyed = true;
    request += 1;
  });
</script>

<div class="memory-history">
  <Button variant="tertiary" aria-expanded={open} onClick={() => (open = !open)}
    >{open ? t('memory.history.hide') : t('memory.history.show')}</Button
  >
  {#if open}
    {#if error}
      <Banner variant="error" role="alert">
        {error}
        <Button variant="secondary" onClick={load}>{t('common.retry')}</Button>
      </Banner>
    {/if}
    {#if loading}
      <p role="status">{t('memory.history.loading')}</p>
    {:else if revisions.length === 0}
      <p>{t('memory.history.empty')}</p>
    {:else}
      <ol class="memory-history__revisions">
        {#each revisions as revision (revision.id)}
          <li>
            <div class="memory-history__heading">
              <span
                >{t('memory.history.revision', { revision: revision.id })}</span
              >
              <time datetime={revision.at}>{dateLabel(revision.at)}</time>
              {#if revision.agent_id}
                <span class="memory-history__author"
                  >{t('memory.history.byAgent', {
                    agent: revision.agent_id,
                  })}</span
                >
              {/if}
              {#if revision.kind !== 'baseline' && revision.changes?.length}
                <Button
                  variant="tertiary"
                  disabled={disabled || reverting}
                  onClick={() => (pendingRevision = revision.id)}
                  >{t('memory.history.revert')}</Button
                >
              {/if}
            </div>
            <ul class="memory-history__changes">
              {#each revision.changes ?? [] as change, index (index)}
                <li>
                  {#if change.op === 'removed' || change.op === 'replaced'}
                    <p>
                      <span>{t('memory.history.removed')}</span>
                      {change.previous ?? change.text}
                    </p>
                  {/if}
                  {#if change.op === 'added' || change.op === 'replaced'}
                    <p>
                      <span>{t('memory.history.added')}</span>
                      {change.text}
                    </p>
                  {/if}
                </li>
              {/each}
            </ul>
          </li>
        {/each}
      </ol>
    {/if}
  {/if}
</div>

{#if pendingRevision !== null}
  <ConfirmDialog
    title={t('memory.history.confirmTitle', { revision: pendingRevision })}
    body={t('memory.history.confirmBody')}
    confirmLabel={t('memory.history.revert')}
    onConfirm={revert}
    onCancel={() => (pendingRevision = null)}
  />
{/if}

<style>
  .memory-history {
    padding: 0 18px 16px;
  }
  .memory-history__revisions,
  .memory-history__changes {
    list-style: none;
    padding: 0;
  }
  .memory-history__revisions > li {
    padding: 12px 0;
    border-top: 1px solid var(--border);
  }
  .memory-history__heading {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }
  time,
  .memory-history__author,
  .memory-history__changes span {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }
  p {
    color: var(--text-med);
    overflow-wrap: anywhere;
  }
</style>
