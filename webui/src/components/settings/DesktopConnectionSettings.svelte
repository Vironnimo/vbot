<script>
  import { onMount, tick } from 'svelte';

  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    addDesktopServer,
    listDesktopServers,
    removeDesktopServer,
    selectDesktopServer,
  } from '$lib/desktopBridge.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let { onToast = noop, idPrefix = 'desktop-settings-server' } = $props();

  let servers = $state([]);
  let loading = $state(true);
  let loadError = $state('');
  let operationError = $state('');
  let adding = $state(false);
  let addOpen = $state(false);
  let connectingKey = $state('');
  let removingKey = $state('');
  let host = $state('');
  let port = $state('8420');
  let label = $state('');
  let formError = $state('');
  let loadGeneration = 0;
  let hostControlId = $derived(`${idPrefix}-host`);
  let portControlId = $derived(`${idPrefix}-port`);
  let labelControlId = $derived(`${idPrefix}-label`);
  let addButtonId = $derived(`${idPrefix}-add`);

  function serverKey(server) {
    return `${server.host}:${server.port}`;
  }

  function serverName(server) {
    return server.label || serverKey(server);
  }

  function errorMessage(error, fallback) {
    const detail = error?.message ? ` ${error.message}` : '';
    return `${fallback}${detail}`;
  }

  async function loadServers() {
    const generation = ++loadGeneration;
    loadError = '';
    try {
      const nextServers = await listDesktopServers();
      if (generation === loadGeneration) {
        servers = nextServers;
      }
    } catch (error) {
      if (generation === loadGeneration) {
        loadError = errorMessage(
          error,
          t('settings.desktop.connection.loadError'),
        );
      }
    } finally {
      if (generation === loadGeneration) {
        loading = false;
      }
    }
  }

  async function openAddForm() {
    addOpen = true;
    await tick();
    document.getElementById(hostControlId)?.focus();
  }

  // Closing discards the unsaved entry and returns focus to "Add server".
  async function closeAddForm() {
    addOpen = false;
    host = '';
    port = '8420';
    label = '';
    formError = '';
    await tick();
    document.getElementById(addButtonId)?.focus();
  }

  async function handleAdd(event) {
    event.preventDefault();
    formError = '';
    operationError = '';

    const normalizedHost = host.trim();
    const numericPort = Number(port);
    if (!normalizedHost) {
      formError = t('settings.desktop.connection.hostRequired');
      return;
    }
    if (
      !Number.isInteger(numericPort) ||
      numericPort < 1 ||
      numericPort > 65535
    ) {
      formError = t('settings.desktop.connection.portInvalid');
      return;
    }

    adding = true;
    try {
      await addDesktopServer(normalizedHost, numericPort, label.trim());
      await loadServers();
      void closeAddForm();
      onToast({
        title: t('settings.desktop.connection.addSuccess'),
        variant: 'success',
      });
    } catch (error) {
      formError = errorMessage(
        error,
        t('settings.desktop.connection.addError'),
      );
    } finally {
      adding = false;
    }
  }

  async function handleConnect(server) {
    const key = serverKey(server);
    operationError = '';
    connectingKey = key;
    try {
      const result = await selectDesktopServer(server.host, server.port);
      if (!result?.url) {
        operationError = [result?.error_title, result?.error_body]
          .filter(Boolean)
          .join(' — ');
      }
    } catch (error) {
      operationError = errorMessage(
        error,
        t('settings.desktop.connection.connectError'),
      );
    } finally {
      connectingKey = '';
    }
  }

  async function handleRemove(server) {
    if (server.active) {
      return;
    }
    const key = serverKey(server);
    operationError = '';
    removingKey = key;
    try {
      await removeDesktopServer(server.host, server.port);
      servers = servers.filter((entry) => serverKey(entry) !== key);
      onToast({
        title: t('settings.desktop.connection.removeSuccess'),
        variant: 'success',
      });
    } catch (error) {
      operationError = errorMessage(
        error,
        t('settings.desktop.connection.removeError'),
      );
    } finally {
      removingKey = '';
    }
  }

  onMount(() => {
    void loadServers();
    return () => {
      loadGeneration += 1;
    };
  });
</script>

<!-- An entity list: a toolbar with the count, the "?" and "Add server", the
     add form as its own group once opened, then one row per saved server. -->
<div class="desktop-connection-settings">
  {#if operationError}
    <Banner variant="error" role="alert">{operationError}</Banner>
  {/if}

  <div class="s-group-toolbar s-list-toolbar">
    {#if !loading && !loadError && servers.length > 0}
      <span class="s-group-toolbar__meta">
        {t('settings.desktop.connection.count', { count: servers.length })}
      </span>
    {/if}
    <InfoHint text={t('settings.desktop.connection.help')} />
    <div class="s-group-toolbar__actions s-list-toolbar__end">
      <Button id={addButtonId} variant="secondary" onClick={openAddForm}>
        {t('settings.desktop.connection.addAction')}
      </Button>
    </div>
  </div>

  {#if addOpen}
    <form
      class="s-group"
      aria-label={t('settings.desktop.connection.addAction')}
      onsubmit={handleAdd}
    >
      <div class="s-group__block desktop-server-form">
        <FormField
          controlId={hostControlId}
          label={t('settings.desktop.connection.host')}
          required
        >
          <TextField
            id={hostControlId}
            value={host}
            placeholder="pi.lan"
            disabled={adding}
            onInput={(next) => (host = next)}
          />
        </FormField>
        <FormField
          controlId={portControlId}
          label={t('settings.desktop.connection.port')}
          required
        >
          <TextField
            id={portControlId}
            value={port}
            inputmode="numeric"
            disabled={adding}
            onInput={(next) => (port = next)}
          />
        </FormField>
        <FormField
          controlId={labelControlId}
          label={t('settings.desktop.connection.label')}
          full
        >
          <TextField
            id={labelControlId}
            value={label}
            placeholder={t('settings.desktop.connection.labelPlaceholder')}
            disabled={adding}
            onInput={(next) => (label = next)}
          />
        </FormField>
        {#if formError}
          <div class="desktop-server-form__error">
            <Banner variant="error" role="alert">
              {formError}
            </Banner>
          </div>
        {/if}
        <div class="desktop-server-form__actions">
          <Button variant="tertiary" disabled={adding} onClick={closeAddForm}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" variant="primary" loading={adding}>
            {adding
              ? t('common.saving')
              : t('settings.desktop.connection.addAction')}
          </Button>
        </div>
      </div>
    </form>
  {/if}

  {#if loadError}
    <Banner variant="error">
      <span>{loadError}</span>
      <Button variant="secondary" onClick={loadServers}>
        {t('common.retry')}
      </Button>
    </Banner>
  {:else if loading}
    <Banner variant="neutral">
      {t('settings.desktop.connection.loading')}
    </Banner>
  {:else if servers.length === 0}
    {#if !addOpen}
      <EmptyState
        density="compact"
        title={t('settings.desktop.connection.emptyTitle')}
        description={t('settings.desktop.connection.emptyDescription')}
      />
    {/if}
  {:else}
    <div class="s-group">
      {#each servers as server (serverKey(server))}
        {@const key = serverKey(server)}
        <div class="s-row s-row--compact desktop-server-row">
          <div class="s-row-info">
            <div class="desktop-server-row__heading">
              <span class="s-row-label">{serverName(server)}</span>
              {#if server.active}
                <StatusChip variant="success">
                  {t('settings.desktop.connection.active')}
                </StatusChip>
              {/if}
            </div>
            <div class="s-row-desc desktop-server-row__address">
              {key}
            </div>
          </div>
          {#if !server.active}
            <div class="s-row-control desktop-server-row__actions">
              <Button
                variant="secondary"
                loading={connectingKey === key}
                disabled={Boolean(connectingKey || removingKey)}
                onClick={() => handleConnect(server)}
              >
                {connectingKey === key
                  ? t('settings.desktop.connection.connecting')
                  : t('settings.desktop.connection.connect')}
              </Button>
              <Button
                variant="danger"
                loading={removingKey === key}
                disabled={Boolean(connectingKey || removingKey)}
                onClick={() => handleRemove(server)}
              >
                {t('common.remove')}
              </Button>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .desktop-connection-settings {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .desktop-server-row__heading {
    display: flex;
    min-width: 0;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-sm);
  }

  .desktop-server-row__address {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
  }

  .desktop-server-row__actions {
    display: flex;
    gap: var(--space-sm);
  }

  .desktop-server-form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 132px;
    gap: var(--space-md);
  }

  :global(.desktop-server-form .form-field--full),
  .desktop-server-form__error,
  .desktop-server-form__actions {
    grid-column: 1 / -1;
  }

  .desktop-server-form__actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-sm);
  }

  @media (max-width: 640px) {
    /* Two actions beside a server name leave it too little width on phones,
       so the actions move under the name. */
    .s-group > .s-row.desktop-server-row {
      grid-template-columns: minmax(0, 1fr);
    }

    .desktop-server-row > .s-row-control.desktop-server-row__actions {
      justify-content: flex-start;
    }

    .desktop-server-form {
      grid-template-columns: 1fr;
    }
  }
</style>
