<script>
  import ProviderConnectModal from './ProviderConnectModal.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    accountDisplayName,
    connectionSupportsAddAccount,
    describeAccountSource,
    describeProvider,
    getAddProviderCandidates,
    getAddableConnections,
    getConfiguredConnections,
    getConnectedProviderItems,
    getConnectionAccounts,
    getProviderItems,
    getPublicConnectionId,
    isAccountUsable,
    isOAuthAccount,
    isOAuthConnection,
    isOAuthDeviceFlowConnection,
    isProcessEnvAccount,
  } from '$lib/settingsView.js';

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
    onHeaderActionChange = noop,
  } = $props();

  export function handleProviderAuthCompleted(event) {
    forwardedAuthEvent = event;
  }

  let refreshingModels = $state(false);
  let modelRefreshMessage = $state('');
  let modelRefreshError = $state('');
  let modalScope = $state(null);
  let forwardedAuthEvent = $state(null);

  let providerItems = $derived(getProviderItems(settings));
  let connectedProviders = $derived(getConnectedProviderItems(settings));
  let addProviderCandidates = $derived(getAddProviderCandidates(settings));
  let hasRefreshEligibleProvider = $derived(
    providerItems.some((provider) => providerAppearsRefreshEligible(provider)),
  );

  $effect(() => {
    if (providerAuthEvent) {
      forwardedAuthEvent = providerAuthEvent;
    }
  });

  $effect(() => {
    if (!visible || !hasRefreshEligibleProvider) {
      onHeaderActionChange(null);
      return;
    }

    onHeaderActionChange({
      refreshing: refreshingModels,
      refresh: refreshModelDatabase,
    });

    return () => onHeaderActionChange(null);
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

  function connectionDescription(connection) {
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
      const result = await rpc('provider.unset_key', {
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
      return disconnectProvider(providerId, connectionId, account, { rpc });
    }

    return rpc('provider.disconnect', {
      provider_id: providerId,
      connection_id: connectionId,
      account,
    });
  }

  async function refreshModelDatabase() {
    if (!hasRefreshEligibleProvider || refreshingModels) {
      return;
    }

    refreshingModels = true;
    modelRefreshMessage = '';
    modelRefreshError = '';

    try {
      const result = await rpc('model.refresh_db');
      applyProviderRefreshResult(result);
      await rpc('model.list');
      modelRefreshMessage = t(
        'settings.providers.refreshSuccess',
        'Model DB updated: {providerCount} providers, {count} models available.',
        refreshSummaryValues(result),
      );
    } catch (error) {
      modelRefreshError = `${t(
        'settings.providers.refreshError',
        'Model DB could not be updated.',
      )} ${error.message}`;
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
  {#if modelRefreshError}
    <div class="s-feedback s-feedback--error">{modelRefreshError}</div>
  {:else if modelRefreshMessage}
    <div class="s-feedback s-feedback--success">
      {modelRefreshMessage}
    </div>
  {/if}

  <div class="s-providers-toolbar">
    <button class="btn-primary" type="button" onclick={openAddProviderModal}>
      {t('settings.providers.add.button', 'Add provider')}
    </button>
  </div>

  {#if connectedProviders.length === 0}
    <div class="s-feedback s-feedback--neutral">
      {t(
        'settings.providers.noneConnected',
        'No providers connected yet. Add one to make its models available.',
      )}
    </div>
  {:else}
    {#each connectedProviders as provider (provider.id)}
      <div class="s-provider-card">
        <div class="s-row s-row--provider">
          <div class="s-row-info">
            <div class="s-row-label">
              {providerDisplayName(provider)}
            </div>
            <div class="s-row-desc">
              {describeProvider(provider, t)}
            </div>
          </div>
        </div>

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
                  {#if getConnectionAccounts(connection).length === 0}
                    <span class="chip chip-green">
                      {t('settings.providers.connected', 'Connected')}
                    </span>
                  {/if}
                  {#if connectionSupportsAddAccount(connection)}
                    <button
                      class="btn-outline"
                      type="button"
                      onclick={() => openAddAccountModal(provider, connection)}
                    >
                      {t(
                        'settings.providers.accounts.addButton',
                        'Add account…',
                      )}
                    </button>
                  {/if}
                </div>
              </div>

              {#if getConnectionAccounts(connection).length > 0}
                <ul class="s-connection-accounts">
                  {#each getConnectionAccounts(connection) as account (account.id)}
                    <li class="s-connection-account-row">
                      <span class="s-connection-account-id">
                        {accountDisplayName(account, t)}
                      </span>
                      <span
                        class="chip {isAccountUsable(account)
                          ? 'chip-green'
                          : 'chip-amber'}"
                      >
                        {isAccountUsable(account)
                          ? t('settings.providers.connected', 'Connected')
                          : t(
                              'settings.providers.accounts.notUsable',
                              'Not usable',
                            )}
                      </span>
                      <span class="s-connection-account-source">
                        {describeAccountSource(account, t)}
                      </span>
                      <div class="s-connection-account-actions">
                        {#if isOAuthDeviceFlowConnection(connection) && isOAuthAccount(account)}
                          <button
                            class="btn-outline"
                            type="button"
                            onclick={() =>
                              disconnectOAuthAccount(
                                provider,
                                connection,
                                account,
                              )}
                          >
                            {t('settings.providers.disconnect', 'Disconnect')}
                          </button>
                        {:else if !isOAuthConnection(connection)}
                          <button
                            class="btn-outline"
                            type="button"
                            onclick={() =>
                              openReplaceKeyModal(
                                provider,
                                connection,
                                account,
                              )}
                          >
                            {t('settings.providers.replaceKey', 'Replace key…')}
                          </button>
                          {#if isProcessEnvAccount(account)}
                            <span
                              class="s-connection-account-locked"
                              title={t(
                                'settings.providers.accounts.removeEnvHint',
                                'This credential comes from the process environment and cannot be removed here.',
                              )}
                            >
                              <button
                                class="btn-outline danger"
                                type="button"
                                disabled
                              >
                                {t('common.remove', 'Remove')}
                              </button>
                            </span>
                          {:else}
                            <button
                              class="btn-outline danger"
                              type="button"
                              onclick={() =>
                                removeApiKey(provider, connection, account)}
                            >
                              {t('common.remove', 'Remove')}
                            </button>
                          {/if}
                        {/if}
                      </div>
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/each}

          {#if getAddableConnections(provider).length > 0}
            <div class="s-provider-add-connection">
              <button
                class="btn-outline"
                type="button"
                onclick={() => openAddConnectionModal(provider)}
              >
                {t('settings.providers.add.connectionButton', 'Add connection')}
              </button>
            </div>
          {/if}
        </div>
      </div>
    {/each}
  {/if}

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.providers.customEndpoint', 'Custom endpoint')}
      </div>
      <div class="s-row-desc">
        {t(
          'settings.providers.customEndpointDescription',
          'OpenAI-compatible custom endpoints remain placeholder-only in this phase.',
        )}
      </div>
    </div>
    <div class="s-row-control">
      <div class="s-row-actions">
        <span class="chip chip-orange"
          >{t('settings.providers.customEndpointStatus', 'Placeholder')}</span
        >
        <button class="btn-outline" type="button" disabled>
          {t('settings.providers.configure', 'Configure…')}
        </button>
      </div>
    </div>
  </div>

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
{/if}
