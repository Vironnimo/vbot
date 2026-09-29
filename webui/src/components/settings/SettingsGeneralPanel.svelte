<script>
  import { onDestroy, untrack } from 'svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import { listClients, updateSettings } from '$lib/api.js';
  import { resolveClientConnectionId } from '$lib/clientIdentity.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import {
    buildClientPresenceRows,
    formatBuildIdentity,
    formatServerHost,
    getDataDirectoryValue,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    settings = null,
    page = 'system',
    clientsRefreshToken = 0,
    onOpenSetupGuide = noop,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  let serverHostValue = $derived(formatServerHost(settings?.general?.server));
  let buildValue = $derived(formatBuildIdentity(settings?.general?.build));
  let buildCopyValue = $derived(
    formatBuildIdentity(settings?.general?.build, { fullRevision: true }),
  );
  let buildKnown = $derived(
    typeof settings?.general?.build?.version === 'string' &&
      settings.general.build.version.length > 0,
  );
  let dataDirectoryValue = $derived(getDataDirectoryValue(settings));
  // Only a real path is worth copying, not the "Unknown" placeholder.
  let dataDirectoryKnown = $derived(
    typeof settings?.general?.data_directory === 'string' &&
      settings.general.data_directory.length > 0,
  );
  let keepAwakeValue = $state(
    untrack(() => settings?.general?.keep_awake === true),
  );
  let timezoneValue = $state(
    untrack(() => settings?.general?.timezone ?? 'UTC'),
  );
  let timezoneOptions = $derived(
    (settings?.general?.available_timezones ?? []).map((timezone) => ({
      value: timezone,
      label: timezone,
    })),
  );
  let baselineKeepAwake = untrack(() => keepAwakeValue);
  let baselineTimezone = untrack(() => timezoneValue);
  $effect(() => {
    const nextKeepAwake = settings?.general?.keep_awake === true;
    const nextTimezone = settings?.general?.timezone ?? 'UTC';
    untrack(() => {
      if (keepAwakeValue === baselineKeepAwake) keepAwakeValue = nextKeepAwake;
      if (timezoneValue === baselineTimezone) timezoneValue = nextTimezone;
      baselineKeepAwake = nextKeepAwake;
      baselineTimezone = nextTimezone;
    });
  });
  let saving = $state(false);
  async function manualSave() {
    const pending = autosave.participant.hasPending();
    if (await autosave.participant.runSave('manual'))
      onToast({
        title: pending ? t('common.saved') : t('common.alreadySaved'),
        variant: 'success',
      });
  }
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () =>
      page === 'preferences' ? timezoneValue : keepAwakeValue,
    hasChanges: () =>
      page === 'preferences'
        ? timezoneValue !== (settings?.general?.timezone ?? 'UTC')
        : keepAwakeValue !== (settings?.general?.keep_awake === true),
    save: async () => {
      onError('');
      saving = true;
      try {
        const server =
          page === 'preferences'
            ? { timezone: timezoneValue }
            : { keep_awake: keepAwakeValue };
        onCommit(await updateSettings({ server }));
        return true;
      } catch (error) {
        onError(`${t('settings.saveError')} ${error.message}`);
        return false;
      } finally {
        saving = false;
      }
    },
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);
  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });
  function handleTimezoneChange(next) {
    timezoneValue = next;
    void autosave.participant.runSave();
  }
  function handleKeepAwakeChange(next) {
    keepAwakeValue = next;
    void autosave.participant.runSave();
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
    if (page === 'system') void loadClients();
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
      )} ${error.message}`;
    }
  }

  function accessorLabel(accessor) {
    if (accessor === 'browser') {
      return t('settings.general.clients.accessor.browser');
    }
    if (accessor === 'desktop') {
      return t('settings.general.clients.accessor.desktop');
    }
    if (accessor === 'tray') {
      return t('settings.general.clients.accessor.tray');
    }
    return t('settings.general.clients.accessor.unknown');
  }

  function connectedAtLabel(connectedAt) {
    if (!connectedAt) {
      return '';
    }
    const date = new Date(connectedAt);
    if (Number.isNaN(date.getTime())) {
      return '';
    }
    return formatDateTimeInApplicationZone(date, activeLocaleTag(), {
      dateStyle: 'medium',
      timeStyle: 'short',
    });
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
        t('settings.general.clients.connectedAt', {
          time: since,
        }),
      );
    }
    return parts.join(' · ');
  }
</script>

{#if page === 'preferences'}
  <div class="s-group">
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.general.timezone')}
          <InfoHint text={t('settings.general.timezoneHelp')} />
        </div>
      </div>
      <div class="s-row-control s-row-control--input">
        <SearchableDropdown
          id="settings-general-timezone"
          value={timezoneValue}
          options={timezoneOptions}
          ariaLabel={t('settings.general.timezone')}
          searchPlaceholder={t('settings.general.timezoneSearch')}
          onValueChange={handleTimezoneChange}
        />
      </div>
    </div>
  </div>

  {@render saveFooter()}

  <!-- The setup guide is an action, not a setting: its own group after the
       region settings. -->
  <div class="s-group">
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.general.setupGuide')}
        </div>
        <div class="s-row-desc">
          {t('settings.general.setupGuideDescription')}
        </div>
      </div>
      <div class="s-row-control">
        <Button
          variant="secondary"
          ariaLabel={t('settings.general.setupGuideAction')}
          onClick={onOpenSetupGuide}
        >
          {t('settings.general.setupGuideOpen')}
        </Button>
      </div>
    </div>
  </div>
{:else}
  <!-- The one setting comes first; the read-only facts about this server
       follow as plain values. -->
  <div class="s-group">
    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.general.keepAwake')}
          <InfoHint text={t('settings.general.keepAwakeHelp')} />
        </div>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={keepAwakeValue}
          ariaLabel={t('settings.general.keepAwake')}
          onChange={handleKeepAwakeChange}
        />
      </div>
    </div>
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.general.version')}
        </div>
      </div>
      <div class="s-row-control server-fact">
        <span class="server-fact__value">{buildValue}</span>
        {#if buildKnown}
          <CopyButton
            class="server-fact__copy"
            text={buildCopyValue}
            label={t('settings.general.copyVersion')}
          />
        {/if}
      </div>
    </div>
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.general.serverHost')}
        </div>
      </div>
      <div class="s-row-control server-fact">
        <span class="server-fact__value">{serverHostValue}</span>
      </div>
    </div>
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.general.dataDirectory')}
          <InfoHint text={t('settings.general.dataDirectoryHelp')} />
        </div>
      </div>
      <div class="s-row-control server-fact">
        <span class="server-fact__value">{dataDirectoryValue}</span>
        {#if dataDirectoryKnown}
          <CopyButton
            class="server-fact__copy"
            text={dataDirectoryValue}
            label={t('settings.general.copyDataDirectory')}
          />
        {/if}
      </div>
    </div>
  </div>

  {@render saveFooter()}

  <div class="s-subhead">
    <h4 class="s-subhead__title">
      {t('settings.general.clients.title')}
    </h4>
  </div>

  {#if clientsError}
    <Banner variant="error">{clientsError}</Banner>
  {:else if !clientsLoaded}
    <Banner variant="neutral">
      {t('settings.general.clients.loading')}
    </Banner>
  {:else if clientRows.length === 0}
    <EmptyState
      density="compact"
      description={t('settings.general.clients.empty')}
    />
  {:else}
    <!-- Every listed client is connected, so rows carry no status chip. -->
    <div class="s-group s-clients-list">
      {#each clientRows as row (row.id)}
        <div
          class="s-row s-row--compact s-client-row"
          class:s-client-row--own={row.isOwn}
        >
          <div class="s-row-info">
            <div class="s-client-row__head">
              <span class="s-row-label">{accessorLabel(row.accessor)}</span>
              {#if row.isOwn}
                <StatusChip variant="info">
                  {t('settings.general.clients.thisWindow')}
                </StatusChip>
              {/if}
            </div>
            <div class="s-row-desc">{clientDetail(row)}</div>
          </div>
        </div>
      {/each}
    </div>
  {/if}
{/if}

<!-- The save state covers only the editable rows (time zone or keep-awake);
     it renders on the section heading line, after those rows in DOM order. -->
{#snippet saveFooter()}
  <div class="s-footer">
    <SaveButton
      class="s-save-button s-save-button--inline"
      {saving}
      pending={autosave.participant.hasChanges()}
      onClick={manualSave}
    />
  </div>
{/snippet}

<style>
  /* Read-only server facts read as plain values, not as input boxes. They
     start where the page's controls start, so they line up with the fields
     on other pages; the copy action sits at the row end. */
  .s-row > .s-row-control.server-fact {
    justify-content: flex-start;
    gap: 8px;
  }

  .server-fact__value {
    flex: 1 1 auto;
    min-width: 0;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .server-fact :global(.server-fact__copy) {
    flex-shrink: 0;
  }
</style>
