<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    describeProvider,
    getConfiguredConnections,
    isConnectionEnabled,
    connectionReachability,
    getConnectionAccounts,
    isKeylessConnection,
    accountDisplayName,
    isAccountUsable,
    describeAccountSource,
    isOAuthDeviceFlowConnection,
    isOAuthAccount,
    isOAuthConnection,
    isProcessEnvAccount,
    connectionSupportsAddAccount,
    getAddableConnections,
    getAddProviderCandidates,
    getConnectedProviderItems,
    getCustomProviderItems,
    getProviderItems,
    getPublicConnectionId,
  } from '$lib/settingsView.js';
  import StatusChip from '../ui/StatusChip.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import OpenRouterRoutingSettings from './OpenRouterRoutingSettings.svelte';
  import ProviderConnectModal from './ProviderConnectModal.svelte';
  import CustomProviderModal from './CustomProviderModal.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import { SvelteSet } from 'svelte/reactivity';
  import {
    deleteCustomProvider,
    disconnectProvider as disconnectProviderRequest,
    listModels,
    refreshModelDatabase as refreshModels,
    setConnectionEnabled as setConnectionEnabledRequest,
    unsetProviderKey,
  } from '$lib/api.js';
  import {
    SURFACE_FORM,
    shouldApplyReloadNow,
  } from '$lib/resourceInvalidation.js';
  import { createLocalProviderModels } from './providers/localModels.svelte.js';

  const noop = () => {};

  let {
    settings,
    visible = false,
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
    onReloadSettings = noop,
    modelsRefreshToken = 0,
  } = $props();
  const localModels = createLocalProviderModels({
    get settings() {
      return settings;
    },
    get onToast() {
      return onToast;
    },
    get onError() {
      return onError;
    },
    get onReloadSettings() {
      return onReloadSettings;
    },
  });

  export function handleProviderAuthCompleted(event) {
    forwardedAuthEvent = event;
  }

  let refreshingModels = $state(false);
  let modalScope = $state(null);
  let forwardedAuthEvent = $state(null);

  // A provider change elsewhere is mirrored here through a settings reload, but
  // held while the key-input modal is open so a live edit is never interrupted.
  let pendingSettingsReload = $state(false);
  let customModalOpen = $state(false);
  let customModalProvider = $state(null);
  let deleteCustomCandidate = $state(null);
  let lastModelsRefreshToken = null;
  // Disclosure state: each provider is one collapsed row; connections,
  // accounts, and the local-context editor live in its sub. The sub stays in
  // the DOM while collapsed so search matches.
  const expandedProviders = new SvelteSet();

  function toggleProviderDetails(provider) {
    if (expandedProviders.has(provider.id)) {
      expandedProviders.delete(provider.id);
    } else {
      expandedProviders.add(provider.id);
    }
  }

  function connectionAccountsUsable(connection) {
    return getConnectionAccounts(connection).some((account) =>
      isAccountUsable(account),
    );
  }

  function connectionStatus(connection) {
    if (!isConnectionEnabled(connection)) {
      return 'disabled';
    }
    if (connectionReachability(connection) === false) {
      return 'unreachable';
    }
    if (
      isKeylessConnection(connection) ||
      getConnectionAccounts(connection).length === 0 ||
      connectionAccountsUsable(connection)
    ) {
      return 'connected';
    }
    return 'not_usable';
  }

  // The collapsed provider row needs a status at a glance: the head chip
  // shows the best state across its connections (connected beats a warning,
  // any warning beats all-disabled).
  function providerSummaryStatus(provider) {
    const statuses = getConfiguredConnections(provider).map(connectionStatus);
    if (statuses.includes('connected')) {
      return 'connected';
    }
    if (statuses.includes('unreachable')) {
      return 'unreachable';
    }
    if (statuses.includes('not_usable')) {
      return 'not_usable';
    }
    return 'disabled';
  }

  function providerSummaryChip(provider) {
    const status = providerSummaryStatus(provider);
    if (status === 'connected') {
      return {
        variant: 'success',
        label: t('settings.providers.connected', 'Connected'),
      };
    }
    if (status === 'unreachable') {
      return {
        variant: 'warn',
        label: t('settings.providers.notReachableChip', 'Not reachable'),
      };
    }
    if (status === 'not_usable') {
      return {
        variant: 'warn',
        label: t('settings.providers.accounts.notUsable', 'Not usable'),
      };
    }
    return {
      variant: 'warn',
      label: t('settings.providers.disabledChip', 'Disabled'),
    };
  }

  let providerItems = $derived(getProviderItems(settings));
  let connectedProviders = $derived(getConnectedProviderItems(settings));
  let displayedProviders = $derived(
    providerItems.filter(
      (provider) =>
        provider?.custom === true ||
        connectedProviders.some((connected) => connected.id === provider.id),
    ),
  );
  let customProviderItems = $derived(getCustomProviderItems(settings));

  // Only providers shipping a keyless (local) connection can have flagged-local
  // models — the model.list fetch for the context editor is skipped otherwise.
  let hasKeylessProvider = $derived(
    providerItems.some((provider) =>
      (provider?.connections ?? []).some((connection) =>
        isKeylessConnection(connection),
      ),
    ),
  );
  let addProviderCandidates = $derived(getAddProviderCandidates(settings));
  let hasRefreshEligibleProvider = $derived(
    providerItems.some((provider) => providerAppearsRefreshEligible(provider)),
  );

  $effect(() => {
    if (providerAuthEvent) {
      forwardedAuthEvent = providerAuthEvent;
    }
  });

  // Load the flagged-local model list when the panel becomes visible and when
  // a model catalog change is signalled (e.g. the Ollama auto-refresh).
  $effect(() => {
    if (visible && hasKeylessProvider) {
      void (modelsRefreshToken, localModels.loadLocalModels());
    }
  });

  // A `resource_changed(models|providers)` signal queues a settings reload so
  // this window reflects the change (first run is a no-op: mount has the prop).
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      pendingSettingsReload = true;
    }
  });

  // Run the queued reload once the key-input modal is closed, so a live
  // credential edit is never swapped out from under the user.
  $effect(() => {
    if (
      pendingSettingsReload &&
      shouldApplyReloadNow(SURFACE_FORM, { focused: modalScope !== null })
    ) {
      pendingSettingsReload = false;
      void onReloadSettings();
    }
  });

  function providerAppearsRefreshEligible(provider) {
    return (
      typeof provider?.models_endpoint === 'string' &&
      provider.models_endpoint.length > 0 &&
      (provider.credentials_configured === true ||
        provider.status === 'configured')
    );
  }

  function providerDisplayName(provider) {
    return provider?.name ?? provider?.id ?? 'Provider';
  }

  let connectionToggleBusy = $state(false);

  async function setConnectionEnabled(provider, connection, enabled) {
    onError('');
    connectionToggleBusy = true;

    try {
      const result = await setConnectionEnabledRequest({
        provider_id: provider.id,
        connection_id: getPublicConnectionId(connection),
        enabled,
      });

      const connectionLabel = connection.label ?? connection.id;
      if (!enabled) {
        onToast({
          title: t(
            'settings.providers.disabledToast',
            '{connection} disabled.',
            {
              connection: connectionLabel,
            },
          ),
          variant: 'success',
        });
      } else if (result?.reachable === false) {
        onToast({
          title: t(
            'settings.providers.enabledUnreachableToast',
            '{connection} enabled, but the endpoint is not reachable. Start the service and its models appear automatically.',
            { connection: connectionLabel },
          ),
          variant: 'warn',
        });
      } else if (result?.reachable === true) {
        onToast({
          title: t(
            'settings.providers.enabledReachableToast',
            '{connection} enabled — endpoint reachable, model catalog refreshed.',
            { connection: connectionLabel },
          ),
          variant: 'success',
        });
      }

      await onReloadSettings();
    } catch (error) {
      onError(
        `${t('settings.providers.toggleError', 'Provider connection could not be updated.')} ${error.message}`,
      );
    } finally {
      connectionToggleBusy = false;
    }
  }

  function connectionDescription(connection) {
    if (!isConnectionEnabled(connection)) {
      return t(
        'settings.providers.disabledDescription',
        'Disabled — not probed and offering no models until you enable it.',
      );
    }
    if (isKeylessConnection(connection)) {
      return t(
        'settings.providers.keylessDescription',
        'No key required — this endpoint is keyless.',
      );
    }
    if (isOAuthDeviceFlowConnection(connection)) {
      return t(
        'settings.providers.oauthDescription',
        'OAuth device authorization managed by the provider.',
      );
    }
    if (isOAuthConnection(connection)) {
      return t(
        'settings.providers.oauthTokenDescription',
        'OAuth token configured from environment or data directory.',
      );
    }
    return t(
      'settings.providers.apiKeyDescription',
      'Static credential configured from environment or data directory.',
    );
  }

  function openAddProviderModal() {
    modalScope = { provider: null, connection: null, account: null };
  }

  function openCustomProviderModal(provider = null) {
    customModalProvider = provider;
    customModalOpen = true;
  }

  function closeCustomProviderModal() {
    customModalOpen = false;
    customModalProvider = null;
  }

  function customProviderSettings(providerId) {
    return (
      customProviderItems.find((provider) => provider.id === providerId) ?? null
    );
  }

  async function reloadAfterCustomProviderSave() {
    await onReloadSettings();
  }

  async function confirmDeleteCustomProvider() {
    if (!deleteCustomCandidate) {
      return;
    }
    const providerId = deleteCustomCandidate.id;
    try {
      await deleteCustomProvider({ provider_id: providerId });
      deleteCustomCandidate = null;
      onToast({
        title: t(
          'settings.providers.custom.deleted',
          'Custom Provider deleted.',
        ),
        variant: 'success',
      });
      await onReloadSettings();
    } catch (error) {
      onError(
        `${t(
          'settings.providers.custom.deleteError',
          'Custom Provider could not be deleted.',
        )} ${error.message}`,
      );
    }
  }

  function openAddConnectionModal(provider) {
    modalScope = { provider, connection: null, account: null };
  }

  function openAddAccountModal(provider, connection) {
    modalScope = { provider, connection, account: null };
  }

  function openReplaceKeyModal(provider, connection, account) {
    modalScope = { provider, connection, account: account.id };
  }

  function closeModal() {
    modalScope = null;
  }

  async function reloadAfterConnect() {
    await onReloadSettings();
  }

  async function disconnectOAuthAccount(provider, connection, account) {
    onError('');

    try {
      await callDisconnectProvider(
        provider.id,
        getPublicConnectionId(connection),
        account.id,
      );
      await onReloadSettings();
    } catch (error) {
      onError(
        `${t('settings.providers.disconnectError', 'Provider connection could not be disconnected.')} ${error.message}`,
      );
    }
  }

  async function removeApiKey(provider, connection, account) {
    onError('');

    try {
      const result = await unsetProviderKey({
        provider_id: provider.id,
        connection_id: getPublicConnectionId(connection),
        account: account.id,
      });

      if (result?.configured === true) {
        onToast({
          title: t(
            'settings.providers.removeKeyStillEnv',
            'Key removed, but the process environment still provides a credential.',
          ),
          variant: 'warn',
        });
      } else {
        onToast({
          title: t('settings.providers.removeKeySuccess', 'API key removed.'),
          variant: 'success',
        });
      }

      await onReloadSettings();
    } catch (error) {
      onError(
        `${t('settings.providers.removeKeyError', 'API key could not be removed.')} ${error.message}`,
      );
    }
  }

  async function callDisconnectProvider(providerId, connectionId, account) {
    if (typeof disconnectProvider === 'function') {
      return disconnectProvider(providerId, connectionId, account);
    }

    return disconnectProviderRequest(providerId, connectionId, account);
  }

  async function refreshModelDatabase() {
    if (!hasRefreshEligibleProvider || refreshingModels) {
      return;
    }

    refreshingModels = true;
    onError('');

    try {
      const result = await refreshModels();
      applyProviderRefreshResult(result);
      await listModels();
      // Success is a toast, not inline text: the refresh triggers a settings
      // reload (resource_changed → onReloadSettings) that briefly unmounts this
      // panel, so an inline result would flash and vanish. The app-level toast
      // survives that reload.
      onToast({
        title: t(
          'settings.providers.refreshSuccess',
          'Model DB updated: {providerCount} providers, {count} models available.',
          refreshSummaryValues(result),
        ),
        variant: 'success',
      });
      const failedProviders = getRefreshFailures(result);
      if (failedProviders.length > 0) {
        onToast({
          title: t(
            'settings.providers.refreshPartial',
            'Some providers could not be reached and were skipped: {providers}.',
            { providers: failedProviders.join(', ') },
          ),
          variant: 'warn',
        });
      }
    } catch (error) {
      onError(
        `${t(
          'settings.providers.refreshError',
          'Model DB could not be updated.',
        )} ${error.message}`,
      );
    } finally {
      refreshingModels = false;
    }
  }

  function applyProviderRefreshResult(result) {
    if (!settings?.providers?.items) {
      return;
    }

    const refreshedProviders = getRefreshedProviders(result);

    if (refreshedProviders.length === 0) {
      return;
    }

    const modelCounts = new Map(
      refreshedProviders
        .filter((provider) => typeof provider?.provider_id === 'string')
        .map((provider) => [provider.provider_id, provider.model_count]),
    );

    onCommit({
      ...settings,
      providers: {
        ...settings.providers,
        items: settings.providers.items.map((provider) =>
          modelCounts.has(provider.id)
            ? { ...provider, model_count: modelCounts.get(provider.id) }
            : provider,
        ),
      },
    });
  }

  function getRefreshedProviders(result) {
    if (Array.isArray(result?.providers)) {
      return result.providers;
    }

    if (typeof result?.provider_id === 'string') {
      return [result];
    }

    return [];
  }

  function getRefreshFailures(result) {
    if (!Array.isArray(result?.errors)) {
      return [];
    }

    return result.errors
      .map((entry) => entry?.connection_id ?? entry?.provider_id)
      .filter((label) => typeof label === 'string' && label.length > 0);
  }

  function refreshSummaryValues(result) {
    const refreshedProviders = getRefreshedProviders(result);
    const modelCount = Number.isFinite(result?.model_count)
      ? result.model_count
      : refreshedProviders.reduce(
          (total, provider) =>
            total +
            (Number.isFinite(provider?.model_count) ? provider.model_count : 0),
          0,
        );

    return {
      providerCount: result?.refreshed_count ?? refreshedProviders.length,
      count: modelCount,
    };
  }
</script>

{#if visible}
  <div class="s-list-head">
    <span class="s-list-head-info">
      {t('settings.providers.connectedCount', '{count} connected', {
        count: displayedProviders.length,
      })}
    </span>
    <div class="s-list-head-actions">
      {#if hasRefreshEligibleProvider}
        <Button
          variant="secondary"
          disabled={refreshingModels}
          tooltip={t(
            'settings.providers.refreshModelsHint',
            'Fetches the current model lists from your connected providers and the public model catalog. Run it when a provider ships new models — your hand-maintained overrides are never touched.',
          )}
          onClick={refreshModelDatabase}
        >
          {refreshingModels
            ? t('settings.providers.refreshingModels', 'Updating…')
            : t('settings.providers.refreshModels', 'Update Model DB')}
        </Button>
      {/if}
      <Button variant="primary" onClick={openAddProviderModal}>
        {t('settings.providers.add.button', 'Add provider')}
      </Button>
      <Button variant="secondary" onClick={() => openCustomProviderModal(null)}>
        {t('settings.providers.custom.addButton', 'Add custom')}
      </Button>
    </div>
  </div>

  {#if displayedProviders.length === 0}
    <EmptyState
      density="compact"
      description={t(
        'settings.providers.noneConnected',
        'No providers connected yet. Add one to make its models available.',
      )}
    />
  {:else}
    {#each displayedProviders as provider (provider.id)}
      <div class="s-provider-card">
        <div class="s-provider-head">
          <div class="s-row-info">
            <div class="s-row-label">
              {providerDisplayName(provider)}
            </div>
            <div class="s-row-desc">
              {describeProvider(provider, t)}
            </div>
          </div>
          <div class="s-row-actions s-row-actions--provider">
            <StatusChip variant={providerSummaryChip(provider).variant}>
              {providerSummaryChip(provider).label}
            </StatusChip>
            <Button
              variant="tertiary"
              icon
              class="s-disclosure-btn"
              ariaLabel={t(
                'settings.providers.detailsAria',
                'Details for {id}',
                {
                  id: provider.id,
                },
              )}
              aria-expanded={expandedProviders.has(provider.id)}
              onClick={() => toggleProviderDetails(provider)}
            >
              ▸
            </Button>
          </div>
        </div>

        <div
          class="s-disclosure-sub"
          hidden={!expandedProviders.has(provider.id)}
        >
          <div class="s-provider-connections">
            {#each getConfiguredConnections(provider) as connection (connection.id)}
              <div class="s-provider-connection-row">
                <div class="s-provider-connection-head">
                  <div class="s-row-info">
                    <div class="s-provider-connection-label">
                      {connection.label ?? connection.id}
                    </div>
                    <div class="s-row-desc">
                      {connectionDescription(connection)}
                    </div>
                  </div>

                  <div class="s-row-actions s-row-actions--provider">
                    {#if !isConnectionEnabled(connection)}
                      <StatusChip variant="warn">
                        {t('settings.providers.disabledChip', 'Disabled')}
                      </StatusChip>
                      <Button
                        variant="secondary"
                        disabled={connectionToggleBusy}
                        ariaLabel={t(
                          'settings.providers.enableAria',
                          'Enable connection {id}',
                          { id: connection.id },
                        )}
                        onClick={() =>
                          setConnectionEnabled(provider, connection, true)}
                      >
                        {t('settings.providers.enable', 'Enable')}
                      </Button>
                    {:else}
                      {#if connectionReachability(connection) === false}
                        <StatusChip variant="warn">
                          {t(
                            'settings.providers.notReachableChip',
                            'Not reachable',
                          )}
                        </StatusChip>
                      {:else if getConnectionAccounts(connection).length === 0 || isKeylessConnection(connection) || connectionAccountsUsable(connection)}
                        <StatusChip variant="success">
                          {t('settings.providers.connected', 'Connected')}
                        </StatusChip>
                      {:else}
                        <StatusChip variant="warn">
                          {t(
                            'settings.providers.accounts.notUsable',
                            'Not usable',
                          )}
                        </StatusChip>
                      {/if}
                      <Button
                        variant="secondary"
                        disabled={connectionToggleBusy}
                        ariaLabel={t(
                          'settings.providers.disableAria',
                          'Disable connection {id}',
                          { id: connection.id },
                        )}
                        onClick={() =>
                          setConnectionEnabled(provider, connection, false)}
                      >
                        {t('settings.providers.disable', 'Disable')}
                      </Button>
                    {/if}
                  </div>
                </div>

                {#if isConnectionEnabled(connection) && !isKeylessConnection(connection)}
                  {#if getConnectionAccounts(connection).length > 0}
                    <ul class="s-connection-accounts">
                      {#each getConnectionAccounts(connection) as account (account.id)}
                        <li class="s-connection-account-row">
                          <span class="s-connection-account-id">
                            {accountDisplayName(account, t)}
                          </span>
                          <StatusChip
                            variant={isAccountUsable(account)
                              ? 'success'
                              : 'warn'}
                          >
                            {isAccountUsable(account)
                              ? t('settings.providers.connected', 'Connected')
                              : t(
                                  'settings.providers.accounts.notUsable',
                                  'Not usable',
                                )}
                          </StatusChip>
                          <span class="s-connection-account-source">
                            {describeAccountSource(account, t)}
                          </span>
                          <div class="s-connection-account-actions">
                            {#if isOAuthDeviceFlowConnection(connection) && isOAuthAccount(account)}
                              <Button
                                variant="secondary"
                                onClick={() =>
                                  disconnectOAuthAccount(
                                    provider,
                                    connection,
                                    account,
                                  )}
                              >
                                {t(
                                  'settings.providers.disconnect',
                                  'Disconnect',
                                )}
                              </Button>
                            {:else if !isOAuthConnection(connection)}
                              <Button
                                variant="secondary"
                                onClick={() =>
                                  openReplaceKeyModal(
                                    provider,
                                    connection,
                                    account,
                                  )}
                              >
                                {t(
                                  'settings.providers.replaceKey',
                                  'Replace key…',
                                )}
                              </Button>
                              {#if isProcessEnvAccount(account)}
                                <span
                                  class="s-connection-account-locked"
                                  use:tooltip={t(
                                    'settings.providers.accounts.removeEnvHint',
                                    'This credential comes from the process environment and cannot be removed here.',
                                  )}
                                >
                                  <Button variant="danger" disabled>
                                    {t('common.remove', 'Remove')}
                                  </Button>
                                </span>
                              {:else}
                                <Button
                                  variant="danger"
                                  onClick={() =>
                                    removeApiKey(provider, connection, account)}
                                >
                                  {t('common.remove', 'Remove')}
                                </Button>
                              {/if}
                            {/if}
                          </div>
                        </li>
                      {/each}
                    </ul>
                  {/if}
                  {#if connectionSupportsAddAccount(connection)}
                    <div class="s-connection-add-account">
                      <Button
                        variant="secondary"
                        onClick={() =>
                          openAddAccountModal(provider, connection)}
                      >
                        {t(
                          'settings.providers.accounts.addButton',
                          'Add account…',
                        )}
                      </Button>
                    </div>
                  {/if}
                {/if}
              </div>
            {/each}

            {#if getAddableConnections(provider).length > 0}
              <div class="s-provider-add-connection">
                <Button
                  variant="secondary"
                  onClick={() => openAddConnectionModal(provider)}
                >
                  {t(
                    'settings.providers.add.connectionButton',
                    'Add connection',
                  )}
                </Button>
              </div>
            {/if}
          </div>

          {#if provider.custom === true}
            <div class="s-provider-add-connection">
              <Button
                variant="secondary"
                onClick={() =>
                  openCustomProviderModal(customProviderSettings(provider.id))}
              >
                {t('common.edit', 'Edit')}
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  deleteCustomCandidate = provider;
                }}
              >
                {t('common.delete', 'Delete')}
              </Button>
            </div>
          {/if}

          {#if provider.id === 'openrouter'}
            <OpenRouterRoutingSettings
              {provider}
              active={expandedProviders.has(provider.id) &&
                providerSummaryStatus(provider) === 'connected'}
              {onReloadSettings}
              {onToast}
              {onError}
            />
          {/if}

          {#if (localModels.localModelsByProvider[provider.id] ?? []).length > 0}
            <div class="s-provider-local-context">
              <div class="s-row-info">
                <div class="s-provider-connection-label">
                  {t(
                    'settings.providers.localContext.title',
                    'Local model context',
                  )}
                </div>
                <div class="s-row-desc">
                  {t(
                    'settings.providers.localContext.description',
                    'The context window vBot budgets against and requests from the local server per call. Empty uses the default (32k, capped at the model max).',
                  )}
                </div>
              </div>
              {#each localModels.localModelsByProvider[provider.id] as model (model.id)}
                <div class="s-local-context-row">
                  <span class="s-local-context-model">{model.model_id}</span>
                  <input
                    class="s-local-context-input"
                    type="number"
                    min="1024"
                    step="1024"
                    placeholder={localModels.localContextPlaceholder(model)}
                    value={localModels.localContextDraftValue(model)}
                    disabled={localModels.localContextBusy}
                    aria-label={t(
                      'settings.providers.localContext.inputLabel',
                      'Context window for {model}',
                      { model: model.model_id },
                    )}
                    onchange={(event) =>
                      localModels.saveLocalContextWindow(
                        model,
                        event.currentTarget.value,
                      )}
                  />
                  {#if model.context_window}
                    <span class="s-local-context-max">
                      {t(
                        'settings.providers.localContext.maxHint',
                        'model max {max}',
                        { max: model.context_window.toLocaleString() },
                      )}
                    </span>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      </div>
    {/each}
  {/if}

  {#if modalScope}
    <ProviderConnectModal
      providers={addProviderCandidates}
      scopedProvider={modalScope.provider}
      scopedConnection={modalScope.connection}
      scopedAccount={modalScope.account ?? null}
      providerAuthEvent={forwardedAuthEvent}
      {connectProvider}
      {disconnectProvider}
      {onToast}
      onCompleted={reloadAfterConnect}
      onClose={closeModal}
    />
  {/if}

  {#if customModalOpen}
    <CustomProviderModal
      provider={customModalProvider}
      {onToast}
      onSaved={reloadAfterCustomProviderSave}
      onClose={closeCustomProviderModal}
    />
  {/if}

  {#if deleteCustomCandidate}
    <ConfirmDialog
      title={t(
        'settings.providers.custom.deleteTitle',
        'Delete Custom Provider?',
      )}
      body={t(
        'settings.providers.custom.deleteBody',
        'The Provider and its stored data-directory API keys are removed. Existing Model references are kept and become unavailable.',
      )}
      confirmLabel={t('common.delete', 'Delete')}
      onConfirm={confirmDeleteCustomProvider}
      onCancel={() => {
        deleteCustomCandidate = null;
      }}
    />
  {/if}
{/if}
