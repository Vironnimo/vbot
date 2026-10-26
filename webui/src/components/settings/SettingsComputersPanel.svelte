<script>
  // Desktop computers registered with this server. A registration outlives its
  // connection, so an offline computer the user no longer uses can be removed;
  // its installation registers again when it shares with this server again.
  import { onMount } from 'svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import { removeComputer } from '$lib/api.js';
  import {
    computerDescription,
    computerName,
    useComputers,
  } from '$lib/computers.svelte.js';
  import { t } from '$lib/i18n.js';

  let { onToast = () => {} } = $props();

  const catalog = useComputers();
  const computers = $derived(
    (catalog?.computers ?? []).filter((computer) => computer.id !== 'server'),
  );
  let removeTarget = $state(null);
  let busy = $state(false);
  let error = $state('');

  onMount(() => {
    void catalog?.refresh();
  });

  function closeDialog() {
    removeTarget = null;
    busy = false;
    error = '';
  }

  async function confirmRemove() {
    const computer = removeTarget;
    if (!computer || busy) return;
    busy = true;
    error = '';
    try {
      await removeComputer(computer.id);
      closeDialog();
      onToast({
        title: t('computers.remove.done', { name: computerName(computer) }),
        variant: 'success',
      });
    } catch (failure) {
      error = failure?.message || t('computers.remove.failed');
      busy = false;
    }
    void catalog?.refresh();
  }
</script>

{#if catalog?.error}
  <Banner variant="error" role="alert">
    <span>{catalog.error}</span>
    <Button variant="secondary" onClick={() => void catalog.refresh()}>
      {t('common.retry')}
    </Button>
  </Banner>
{:else if !catalog?.loaded}
  <Banner variant="neutral">{t('common.loading')}</Banner>
{:else if computers.length === 0}
  <EmptyState density="compact" description={t('computers.settingsEmpty')} />
{:else}
  <div class="s-group" role="list" aria-label={t('computers.settingsTitle')}>
    {#each computers as computer (computer.id)}
      <div class="s-row s-row--compact" role="listitem">
        <div class="s-row-info">
          <div class="computer-row__head">
            <span class="s-row-label">{computerName(computer)}</span>
            {#if computer.online}
              <StatusChip variant="success">{t('computers.online')}</StatusChip>
            {/if}
          </div>
          <div class="s-row-desc">{computerDescription(computer)}</div>
        </div>
        {#if !computer.online}
          <div class="s-row-control">
            <Button
              variant="secondary"
              ariaLabel={t('computers.remove.aria', {
                name: computerName(computer),
              })}
              onClick={() => {
                error = '';
                removeTarget = computer;
              }}
            >
              {t('computers.remove.button')}
            </Button>
          </div>
        {/if}
      </div>
    {/each}
  </div>
{/if}

{#if removeTarget}
  <ConfirmDialog
    title={t('computers.remove.title', { name: computerName(removeTarget) })}
    body={t('computers.remove.body')}
    confirmLabel={t('computers.remove.confirm')}
    {busy}
    {error}
    onConfirm={confirmRemove}
    onCancel={closeDialog}
  />
{/if}

<style>
  .computer-row__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
</style>
