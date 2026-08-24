<script>
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { listClients, updateSettings } from '$lib/api.js';
  import { resolveClientConnectionId } from '$lib/clientIdentity.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import {
    buildClientPresenceRows,
    formatServerHost,
    getDataDirectoryValue,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    settings = null,
    clientsRefreshToken = 0,
    onOpenSetupGuide = noop,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  let serverHostValue = $derived(
    formatServerHost(settings?.general?.server, t),
  );
  let dataDirectoryValue = $derived(getDataDirectoryValue(settings, t));
  let keepAwakeValue = $derived(settings?.general?.keep_awake === true);
  let savingKeepAwake = $state(false);

  async function handleKeepAwakeChange(next) {
    if (savingKeepAwake || next === keepAwakeValue) {
      return;
    }
    savingKeepAwake = true;
    onError('');
    try {
      const nextSettings = await updateSettings({
        server: { keep_awake: next },
      });
      onCommit(nextSettings);
      onToast({
        title: t('settings.general.keepAwake', 'Keep computer awake'),
        variant: 'success',
      });
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      savingKeepAwake = false;
    }
  }

  // This window's own presence id — matches the row the WebSocket registered so
  // we can mark "this window". Resolved once; stable for the tab.
  const ownConnectionId = resolveClientConnectionId();

  let clientRows = $state([]);
  let clientsLoaded = $state(false);
  let clientsError = $state('');

  // Reload the roster on mount and on every clients signal App bumps (a window
  // connected or disconnected). A pure display surface, so it swaps immediately.
  // Only the first load shows a spinner; later reloads keep the current roster
  // visible and swap silently, so a live update never flashes "Loading…".
  $effect(() => {
    void clientsRefreshToken;
    void loadClients();
  });

  async function loadClients() {
    clientsError = '';
    try {
      const result = await listClients();
      clientRows = buildClientPresenceRows(
        result?.clients ?? [],
        ownConnectionId,
      );
      clientsLoaded = true;
    } catch (error) {
      clientsError = `${t(
        'settings.general.clients.loadError',
        'Connected clients could not be loaded.',
      )} ${error.message}`;
    }
  }

  function accessorLabel(accessor) {
    if (accessor === 'browser') {
      return t('settings.general.clients.accessor.browser', 'Browser');
    }
    if (accessor === 'desktop') {
      return t('settings.general.clients.accessor.desktop', 'Desktop');
    }
    return t('settings.general.clients.accessor.unknown', 'Unknown');
  }

  function statusLabel(status) {
    if (status === 'connected') {
      return t('settings.general.clients.status.connected', 'Connected');
    }
    return status;
  }

  function connectedAtLabel(connectedAt) {
    if (!connectedAt) {
      return '';
    }
    const date = new Date(connectedAt);
    if (Number.isNaN(date.getTime())) {
      return '';
    }
    return new Intl.DateTimeFormat(activeLocaleTag(), {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date);
  }

  function clientDetail(row) {
    const parts = [];
    const device = [row.browser, row.os].filter(
      (part) => typeof part === 'string' && part.length > 0,
    );
    if (device.length > 0) {
      parts.push(device.join(' · '));
    }
    const since = connectedAtLabel(row.connectedAt);
    if (since) {
      parts.push(
        t('settings.general.clients.connectedAt', 'Connected {time}', {
          time: since,
        }),
      );
    }
    return parts.join(' · ');
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.serverHost', 'Server host')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.serverHostDescription',
        'Address and port the vBot server listens on.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--input">
    <TextField readonly value={serverHostValue} />
  </div>
</div>
<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.dataDirectory', 'Data directory')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.dataDirectoryDescription',
        'Root path for agents, sessions, and workspace files.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--input">
    <TextField readonly value={dataDirectoryValue} />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.keepAwake', 'Keep computer awake')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.keepAwakeDescription',
        'Prevent automatic sleep while vBot is running, so channels such as Telegram stay reachable. Manual sleep still works.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Toggle
      checked={keepAwakeValue}
      disabled={savingKeepAwake}
      ariaLabel={t('settings.general.keepAwake', 'Keep computer awake')}
      onChange={handleKeepAwakeChange}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.setupGuide', 'Setup guide')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.setupGuideDescription',
        'Reopen the guided first-run setup to connect a provider and assign a model.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Button variant="secondary" onClick={onOpenSetupGuide}>
      {t('settings.general.setupGuideAction', 'Open setup guide')}
    </Button>
  </div>
</div>

<div class="s-row s-row--stacked">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.general.clients.title', 'Connected clients')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.general.clients.description',
        'App windows currently connected to this server (browser tabs and the Desktop app).',
      )}
    </div>
  </div>
</div>

{#if clientsError}
  <Banner variant="error">{clientsError}</Banner>
{:else if !clientsLoaded}
  <Banner variant="neutral">
    {t('settings.general.clients.loading', 'Loading connected clients…')}
  </Banner>
{:else if clientRows.length === 0}
  <EmptyState
    density="compact"
    description={t(
      'settings.general.clients.empty',
      'No app windows connected.',
    )}
  />
{:else}
  <div class="s-clients-list">
    {#each clientRows as row (row.id)}
      <div class="s-client-row" class:s-client-row--own={row.isOwn}>
        <div class="s-row-info">
          <div class="s-client-row__head">
            <span class="s-row-label">{accessorLabel(row.accessor)}</span>
            {#if row.isOwn}
              <StatusChip variant="info">
                {t('settings.general.clients.thisWindow', 'This window')}
              </StatusChip>
            {/if}
          </div>
          <div class="s-row-desc">{clientDetail(row)}</div>
        </div>
        <div class="s-row-control">
          <StatusChip variant="success">{statusLabel(row.status)}</StatusChip>
        </div>
      </div>
    {/each}
  </div>
{/if}
