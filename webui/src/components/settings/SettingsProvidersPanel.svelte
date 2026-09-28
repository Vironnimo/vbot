<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    describeProvider,
    isSharedOpenCodeConnection,
    describeSharedOpenCodeKey,
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
  import { shouldApplyReloadNow } from '$lib/resourceInvalidation.js';
  import { createLocalProviderModels } from './providers/localModels.svelte.js';

  const noop = () => {};

  let {
    settings,
    visible = false,
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onCommitProviderSettings = noop,
    onToast = noop,
    onError = noop,
    onRefreshProviderSettings = noop,
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
    get onRefreshProviderSettings() {
      return onRefreshProviderSettings;
    },
  });

  export function handleProviderAuthCompleted(event) {
    forwardedAuthEvent = event;
  }

  let refreshingModels = $state(false);
  let modalScope = $state(null);
  let forwardedAuthEvent = $state(null);

  // A provider change elsewhere refreshes the Provider projection, but is
  // held while the key-input modal is open so a live edit is never interrupted.
  let pendingProviderRefresh = $state(false);
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
        label: t('settings.providers.connected'),
      };
    }
    if (status === 'unreachable') {
      return {
        variant: 'warn',
        label: t('settings.providers.notReachableChip'),
      };
    }
    if (status === 'not_usable') {
      return {
        variant: 'warn',
        label: t('settings.providers.accounts.notUsable'),
      };
    }
    return {
      variant: 'warn',
      label: t('settings.providers.disabledChip'),
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

  // A `resource_changed(models|providers)` signal queues a Provider refresh so
  // this window reflects the change (first run is a no-op: mount has the prop).
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      pendingProviderRefresh = true;
    }
  });

  // Run the queued refresh once the key-input modal is closed, so a live
  // credential edit is never swapped out from under the user.
  $effect(() => {
    if (
      pendingProviderRefresh &&
      shouldApplyReloadNow({ focused: modalScope !== null })
    ) {
      pendingProviderRefresh = false;
      void Promise.resolve(onRefreshProviderSettings()).catch((error) => {
        onError(`${t('settings.loadError')} ${error.message}`);
      });
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
          title: t('settings.providers.disabledToast', {
            connection: connectionLabel,
          }),
          variant: 'success',
        });
      } else if (result?.reachable === false) {
        onToast({
          title: t('settings.providers.enabledUnreachableToast', {
            connection: connectionLabel,
          }),
          variant: 'warn',
        });
      } else if (result?.reachable === true) {
        onToast({
          title: t('settings.providers.enabledReachableToast', {
            connection: connectionLabel,
          }),
          variant: 'success',
        });
      }

      await onRefreshProviderSettings();
    } catch (error) {
      onError(`${t('settings.providers.toggleError')} ${error.message}`);
    } finally {
      connectionToggleBusy = false;
    }
  }

  function connectionDescription(connection) {
    if (isSharedOpenCodeConnection(connection)) {
      return describeSharedOpenCodeKey(t);
    }
    if (!isConnectionEnabled(connection)) {
      return t('settings.providers.disabledDescription');
    }
    if (isKeylessConnection(connection)) {
      return t('settings.providers.keylessDescription');
    }
    if (isOAuthDeviceFlowConnection(connection)) {
      return t('settings.providers.oauthDescription');
    }
    if (isOAuthConnection(connection)) {
      return t('settings.providers.oauthTokenDescription');
    }
    return t('settings.providers.apiKeyDescription');
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
    await onRefreshProviderSettings();
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
        title: t('settings.providers.custom.deleted'),
        variant: 'success',
      });
      await onRefreshProviderSettings();
    } catch (error) {
      onError(`${t('settings.providers.custom.deleteError')} ${error.message}`);
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
    await onRefreshProviderSettings();
  }

  async function disconnectOAuthAccount(provider, connection, account) {
    onError('');

    try {
      await callDisconnectProvider(
        provider.id,
        getPublicConnectionId(connection),
        account.id,
      );
      await onRefreshProviderSettings();
    } catch (error) {
      onError(`${t('settings.providers.disconnectError')} ${error.message}`);
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
          title: t('settings.providers.removeKeyStillEnv'),
          variant: 'warn',
        });
      } else {
        onToast({
          title: t('settings.providers.removeKeySuccess'),
          variant: 'success',
        });
      }

      await onRefreshProviderSettings();
    } catch (error) {
      onError(`${t('settings.providers.removeKeyError')} ${error.message}`);
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
      // The operation result remains visible through the app-level toast while
      // Provider invalidation refreshes the catalog in place.
      const { providerCount, count } = refreshSummary(result);
      onToast({
        title: t('settings.providers.refreshSuccess', { providerCount, count }),
        variant: 'success',
      });
      const failedProviders = getRefreshFailures(result);
      if (failedProviders.length > 0) {
        onToast({
          title: t('settings.providers.refreshPartial', {
            providers: failedProviders.join(', '),
          }),
          variant: 'warn',
        });
      }
    } catch (error) {
      onError(`${t('settings.providers.refreshError')} ${error.message}`);
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

    onCommitProviderSettings({
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

  function refreshSummary(result) {
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

{#snippet refreshModelsButton()}
  <Button
    variant="tertiary"
    disabled={refreshingModels}
    tooltip={t('settings.providers.refreshModelsHint')}
    onClick={refreshModelDatabase}
  >
    {refreshingModels
      ? t('settings.providers.refreshingModels')
      : t('settings.providers.refreshModels')}
  </Button>
{/snippet}

{#if visible}
  {#if displayedProviders.length === 0}
    <EmptyState
      density="compact"
      description={t('settings.providers.noneConnected')}
    >
      {#snippet actions()}
        <Button variant="primary" onClick={openAddProviderModal}>
          {t('settings.providers.add.button')}
        </Button>
        <Button
          variant="secondary"
          onClick={() => openCustomProviderModal(null)}
        >
          {t('settings.providers.custom.addButton')}
        </Button>
        {#if hasRefreshEligibleProvider}
          {@render refreshModelsButton()}
        {/if}
      {/snippet}
    </EmptyState>
  {:else}
    <!-- The count and the catalog refresh describe the whole list; adding
         sits at the far end of the same line. -->
    <div class="s-group-toolbar s-list-toolbar">
      <span class="s-group-toolbar__meta">
        {t('settings.providers.connectedCount', {
          count: displayedProviders.length,
        })}
      </span>
      {#if hasRefreshEligibleProvider}
        {@render refreshModelsButton()}
      {/if}
      <div class="s-group-toolbar__actions s-list-toolbar__end">
        <Button
          variant="tertiary"
          onClick={() => openCustomProviderModal(null)}
        >
          {t('settings.providers.custom.addButton')}
        </Button>
        <Button variant="primary" onClick={openAddProviderModal}>
          {t('settings.providers.add.button')}
        </Button>
      </div>
    </div>

    <div class="s-group s-provider-list">
      {#each displayedProviders as provider (provider.id)}
        {@const expanded = expandedProviders.has(provider.id)}
        <div class="s-provider-card s-entity">
          <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions (Pointer shortcut for the row; the Details disclosure button is the keyboard control.) -->
          <div
            class="s-provider-head s-entity__head s-provider-head--toggle"
            onclick={(event) => {
              if (
                !event.target.closest('button, a, input, select, textarea') &&
                !window.getSelection()?.toString()
              ) {
                toggleProviderDetails(provider);
              }
            }}
          >
            <div class="s-row-info">
              <div class="s-row-label">
                {providerDisplayName(provider)}
              </div>
              <div class="s-row-desc">
                {describeProvider(provider, t)}
              </div>
            </div>
            <div class="s-entity__end">
              <StatusChip variant={providerSummaryChip(provider).variant}>
                {providerSummaryChip(provider).label}
              </StatusChip>
              <Button
                variant="tertiary"
                icon
                class="s-disclosure-btn"
                ariaLabel={t('settings.providers.detailsAria', {
                  id: provider.id,
                })}
                aria-expanded={expanded}
                onClick={() => toggleProviderDetails(provider)}
              >
                <span
                  class="disclosure-chevron"
                  class:disclosure-chevron--open={expanded}
                  aria-hidden="true"
                ></span>
              </Button>
            </div>
          </div>

          <div class="s-disclosure-sub" hidden={!expanded}>
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

                    <div class="s-entity__end">
                      {#if !isConnectionEnabled(connection)}
                        <StatusChip variant="warn">
                          {t('settings.providers.disabledChip')}
                        </StatusChip>
                        <Button
                          variant="secondary"
                          disabled={connectionToggleBusy}
                          ariaLabel={t('settings.providers.enableAria', {
                            id: connection.id,
                          })}
                          onClick={() =>
                            setConnectionEnabled(provider, connection, true)}
                        >
                          {t('settings.providers.enable')}
                        </Button>
                      {:else}
                        {#if connectionReachability(connection) === false}
                          <StatusChip variant="warn">
                            {t('settings.providers.notReachableChip')}
                          </StatusChip>
                        {:else if getConnectionAccounts(connection).length === 0 || isKeylessConnection(connection) || connectionAccountsUsable(connection)}
                          <StatusChip variant="success">
                            {t('settings.providers.connected')}
                          </StatusChip>
                        {:else}
                          <StatusChip variant="warn">
                            {t('settings.providers.accounts.notUsable')}
                          </StatusChip>
                        {/if}
                        <Button
                          variant="secondary"
                          disabled={connectionToggleBusy}
                          ariaLabel={t('settings.providers.disableAria', {
                            id: connection.id,
                          })}
                          onClick={() =>
                            setConnectionEnabled(provider, connection, false)}
                        >
                          {t('settings.providers.disable')}
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
                                ? t('settings.providers.connected')
                                : t('settings.providers.accounts.notUsable')}
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
                                  {t('settings.providers.disconnect')}
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
                                  {t('settings.providers.replaceKey')}
                                </Button>
                                {#if isProcessEnvAccount(account)}
                                  <span
                                    class="s-connection-account-locked"
                                    use:tooltip={t(
                                      'settings.providers.accounts.removeEnvHint',
                                    )}
                                  >
                                    <Button variant="danger" disabled>
                                      {t('common.remove')}
                                    </Button>
                                  </span>
                                {:else}
                                  <Button
                                    variant="danger"
                                    onClick={() =>
                                      removeApiKey(
                                        provider,
                                        connection,
                                        account,
                                      )}
                                  >
                                    {isSharedOpenCodeConnection(connection)
                                      ? t(
                                          'settings.providers.opencode.removeKey',
                                        )
                                      : t('common.remove')}
                                  </Button>
                                {/if}
                              {/if}
                            </div>
                          </li>
                        {/each}
                      </ul>
                    {/if}
                    {#if connectionSupportsAddAccount(connection)}
                      <div class="s-provider-inline-actions">
                        <Button
                          variant="tertiary"
                          onClick={() =>
                            openAddAccountModal(provider, connection)}
                        >
                          {t('settings.providers.accounts.addButton')}
                        </Button>
                      </div>
                    {/if}
                  {/if}
                </div>
              {/each}
            </div>

            {#if getAddableConnections(provider).length > 0 || provider.custom === true}
              <div class="s-provider-inline-actions">
                {#if getAddableConnections(provider).length > 0}
                  <Button
                    variant="secondary"
                    onClick={() => openAddConnectionModal(provider)}
                  >
                    {t('settings.providers.add.connectionButton')}
                  </Button>
                {/if}
                {#if provider.custom === true}
                  <Button
                    variant="secondary"
                    onClick={() =>
                      openCustomProviderModal(
                        customProviderSettings(provider.id),
                      )}
                  >
                    {t('common.edit')}
                  </Button>
                  <Button
                    variant="danger"
                    onClick={() => {
                      deleteCustomCandidate = provider;
                    }}
                  >
                    {t('common.delete')}
                  </Button>
                {/if}
              </div>
            {/if}

            {#if provider.id === 'openrouter'}
              <OpenRouterRoutingSettings
                {provider}
                active={expanded &&
                  providerSummaryStatus(provider) === 'connected'}
                {onRefreshProviderSettings}
                {onToast}
                {onError}
              />
            {/if}

            {#if (localModels.localModelsByProvider[provider.id] ?? []).length > 0}
              <div class="s-provider-local-context">
                <div class="s-row-info">
                  <div class="s-provider-connection-label">
                    {t('settings.providers.localContext.title')}
                  </div>
                  <div class="s-row-desc">
                    {t('settings.providers.localContext.description')}
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
                        {t('settings.providers.localContext.maxHint', {
                          max: model.context_window.toLocaleString(),
                        })}
                      </span>
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        </div>
      {/each}
    </div>
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
      title={t('settings.providers.custom.deleteTitle')}
      body={t('settings.providers.custom.deleteBody')}
      confirmLabel={t('common.delete')}
      onConfirm={confirmDeleteCustomProvider}
      onCancel={() => {
        deleteCustomCandidate = null;
      }}
    />
  {/if}
{/if}
