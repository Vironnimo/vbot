<script>
  import { onMount } from 'svelte';

  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
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
          t(
            'settings.desktop.connection.loadError',
            'Saved servers could not be loaded.',
          ),
        );
      }
    } finally {
      if (generation === loadGeneration) {
        loading = false;
      }
    }
  }

  async function handleAdd(event) {
    event.preventDefault();
    formError = '';
    operationError = '';

    const normalizedHost = host.trim();
    const numericPort = Number(port);
    if (!normalizedHost) {
      formError = t(
        'settings.desktop.connection.hostRequired',
        'Enter a server host.',
      );
      return;
    }
    if (
      !Number.isInteger(numericPort) ||
      numericPort < 1 ||
      numericPort > 65535
    ) {
      formError = t(
        'settings.desktop.connection.portInvalid',
        'Enter a port between 1 and 65535.',
      );
      return;
    }

    adding = true;
    try {
      await addDesktopServer(normalizedHost, numericPort, label.trim());
      host = '';
      port = '8420';
      label = '';
      await loadServers();
      onToast({
        title: t('settings.desktop.connection.addSuccess', 'Server saved.'),
        variant: 'success',
      });
    } catch (error) {
      formError = errorMessage(
        error,
        t('settings.desktop.connection.addError', 'Server could not be saved.'),
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
        t(
          'settings.desktop.connection.connectError',
          'The Desktop app could not connect to that server.',
        ),
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
        title: t(
          'settings.desktop.connection.removeSuccess',
          'Server removed.',
        ),
        variant: 'success',
      });
    } catch (error) {
      operationError = errorMessage(
        error,
        t(
          'settings.desktop.connection.removeError',
          'Server could not be removed.',
        ),
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

<!-- Two sub-topics: the saved servers as one group of rows, then the form
     that adds one. -->
<div class="desktop-connection-settings">
  {#if operationError}
    <Banner variant="error" role="alert">{operationError}</Banner>
  {/if}

  <div class="s-subhead desktop-connection-subhead">
    <h4 class="s-subhead__title">
      {t('settings.desktop.connection.savedTitle', 'Saved servers')}
    </h4>
    <p class="s-subhead__desc">
      {t(
        'settings.desktop.connection.savedDescription',
        'The active server supplies this WebUI. Switching reloads the Desktop app without moving Sessions or Runs.',
      )}
    </p>
  </div>

  {#if loadError}
    <Banner variant="error">
      <span>{loadError}</span>
      <Button variant="secondary" onClick={loadServers}>
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {:else if loading}
    <Banner variant="neutral">
      {t('settings.desktop.connection.loading', 'Loading saved servers…')}
    </Banner>
  {:else if servers.length === 0}
    <EmptyState
      density="compact"
      title={t('settings.desktop.connection.emptyTitle', 'No saved servers')}
      description={t(
        'settings.desktop.connection.emptyDescription',
        'Add a server below to make it available for this Desktop app.',
      )}
    />
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
                  {t('settings.desktop.connection.active', 'Connected')}
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
                  ? t('settings.desktop.connection.connecting', 'Connecting…')
                  : t('settings.desktop.connection.connect', 'Connect')}
              </Button>
              <Button
                variant="danger"
                loading={removingKey === key}
                disabled={Boolean(connectingKey || removingKey)}
                onClick={() => handleRemove(server)}
              >
                {t('common.remove', 'Remove')}
              </Button>
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <div class="s-subhead">
    <h4 class="s-subhead__title">
      {t('settings.desktop.connection.addTitle', 'Add server')}
    </h4>
    <p class="s-subhead__desc">
      {t(
        'settings.desktop.connection.addDescription',
        'Save a local or remote vBot server for this Windows app.',
      )}
    </p>
  </div>

  <form class="s-group" onsubmit={handleAdd}>
    <div class="s-group__block desktop-server-form">
      <FormField
        controlId={hostControlId}
        label={t('settings.desktop.connection.host', 'Host')}
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
        label={t('settings.desktop.connection.port', 'Port')}
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
        label={t('settings.desktop.connection.label', 'Label (optional)')}
        full
      >
        <TextField
          id={labelControlId}
          value={label}
          placeholder={t(
            'settings.desktop.connection.labelPlaceholder',
            'Home server',
          )}
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
        <Button type="submit" variant="primary" loading={adding}>
          {adding
            ? t('common.saving', 'Saving…')
            : t('settings.desktop.connection.addAction', 'Add server')}
        </Button>
      </div>
    </div>
  </form>
</div>

<style>
  .desktop-connection-settings {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  /* The first sub-topic starts right under the section heading. */
  .desktop-connection-subhead {
    margin-top: 0;
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
