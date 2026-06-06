<script>
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    describeProvider,
    getOAuthConnectionStatus,
    getProviderItems,
    getPublicConnectionId,
    isOAuthConnection,
    isOAuthDeviceFlowConnection,
    providerStatusClass,
    providerStatusLabel,
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
    handleProviderAuthEvent(event);
  }

  let refreshingModels = $state(false);
  let modelRefreshMessage = $state('');
  let modelRefreshError = $state('');
  let oauthConnectionStates = $state({});
  let handledProviderAuthEvent = null;
  let copiedDeviceFlowConnectionId = $state('');

  let providerItems = $derived(getProviderItems(settings));
  let hasRefreshEligibleProvider = $derived(
    providerItems.some((provider) => providerAppearsRefreshEligible(provider)),
  );

  $effect(() => {
    if (providerAuthEvent && providerAuthEvent !== handledProviderAuthEvent) {
      handledProviderAuthEvent = providerAuthEvent;
      handleProviderAuthEvent(providerAuthEvent);
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

  function getOAuthState(connectionId) {
    return (
      oauthConnectionStates[connectionId] ?? {
        flowActive: false,
        showDialog: false,
        dialogData: null,
      }
    );
  }

  function updateOAuthState(connectionId, patch) {
    oauthConnectionStates = {
      ...oauthConnectionStates,
      [connectionId]: {
        ...getOAuthState(connectionId),
        ...patch,
      },
    };
  }

  function isConnectionConfigured(connection) {
    return connection?.configured === true || connection?.usable === true;
  }

  function oauthStatus(connection) {
    return getOAuthConnectionStatus(
      providerItems,
      connection.id,
      getOAuthState(connection.id).flowActive,
    );
  }

  function providerDisplayName(provider) {
    return provider?.name ?? provider?.id ?? 'Provider';
  }

  function providerTranslationValues(provider) {
    return { provider: providerDisplayName(provider) };
  }

  async function startOAuthConnect(provider, connection) {
    const connectionId = getPublicConnectionId(connection);

    onError('');
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connection.id, {
      flowActive: true,
      showDialog: false,
      dialogData: null,
    });

    try {
      const response = await callConnectProvider(provider.id, connectionId);
      updateOAuthState(connection.id, {
        flowActive: true,
        showDialog: Boolean(response?.user_code),
        dialogData: response,
      });
    } catch (error) {
      updateOAuthState(connection.id, {
        flowActive: false,
        showDialog: false,
        dialogData: null,
      });
      onError(
        `${t('settings.providers.connectError', 'Provider connection could not be started.')} ${error.message}`,
      );
    }
  }

  async function cancelOAuthFlow(provider, connection) {
    await disconnectOAuthProvider(provider, connection, { reload: false });
  }

  async function disconnectOAuthProvider(provider, connection, options = {}) {
    const connectionId = getPublicConnectionId(connection);
    onError('');
    copiedDeviceFlowConnectionId = '';

    try {
      await callDisconnectProvider(provider.id, connectionId);
      updateOAuthState(connection.id, {
        flowActive: false,
        showDialog: false,
        dialogData: null,
      });

      if (options.reload ?? true) {
        await onReloadSettings();
      }
    } catch (error) {
      onError(
        `${t('settings.providers.disconnectError', 'Provider connection could not be disconnected.')} ${error.message}`,
      );
    }
  }

  async function completeOAuthFlow(connectionId, provider) {
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connectionId, {
      flowActive: false,
      showDialog: false,
      dialogData: null,
    });
    showSettingsToast(
      t(
        'settings.providers.device_flow.success_toast',
        '{provider} connected successfully',
        providerTranslationValues(provider),
      ),
      'success',
    );
    await onReloadSettings();
  }

  function failOAuthFlow(connectionId) {
    copiedDeviceFlowConnectionId = '';
    updateOAuthState(connectionId, {
      flowActive: false,
      showDialog: false,
      dialogData: null,
    });
    showSettingsToast(
      t(
        'settings.providers.device_flow.error_toast',
        'Authorization failed or timed out',
      ),
      'error',
    );
  }

  function showSettingsToast(message, variant = 'success') {
    onToast?.({ title: message, variant });
  }

  async function copyDeviceFlowUserCode(connection, userCode) {
    if (!userCode) {
      return;
    }

    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
      showSettingsToast(
        t(
          'settings.providers.device_flow.copy_error',
          'Device code could not be copied.',
        ),
        'error',
      );
      return;
    }

    try {
      await navigator.clipboard.writeText(userCode);
      copiedDeviceFlowConnectionId = connection.id;
      showSettingsToast(
        t('settings.providers.device_flow.copy_success', 'Device code copied.'),
        'success',
      );
    } catch {
      showSettingsToast(
        t(
          'settings.providers.device_flow.copy_error',
          'Device code could not be copied.',
        ),
        'error',
      );
    }
  }

  async function callConnectProvider(providerId, connectionId) {
    if (typeof connectProvider === 'function') {
      return connectProvider(providerId, connectionId, { rpc });
    }

    return rpc('provider.connect', {
      provider_id: providerId,
      connection_id: connectionId,
    });
  }

  async function callDisconnectProvider(providerId, connectionId) {
    if (typeof disconnectProvider === 'function') {
      return disconnectProvider(providerId, connectionId, { rpc });
    }

    return rpc('provider.disconnect', {
      provider_id: providerId,
      connection_id: connectionId,
    });
  }

  function handleProviderAuthEvent(event) {
    const payload = event.payload ?? event;
    const connectionContext = findConnectionContext(
      payload.provider_id,
      payload.connection_id,
    );
    const connectionStateId = connectionContext.connectionStateId;

    if (!connectionStateId || !getOAuthState(connectionStateId).flowActive) {
      return;
    }

    if (payload.success === true) {
      completeOAuthFlow(connectionStateId, connectionContext.provider);
      return;
    }

    failOAuthFlow(connectionStateId);
  }

  function findConnectionContext(providerId, connectionId) {
    const provider = providerItems.find((item) => item.id === providerId);
    const connections = Array.isArray(provider?.connections)
      ? provider.connections
      : [];
    const connection = connections.find(
      (item) => getPublicConnectionId(item) === connectionId,
    );

    return {
      provider,
      connection,
      connectionStateId: connection?.id ?? '',
    };
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

  {#if providerItems.length === 0}
    <div class="s-feedback s-feedback--neutral">
      {t('settings.providers.empty', 'No providers are available.')}
    </div>
  {:else}
    {#each providerItems as provider (provider.id)}
      <div class="s-provider-card">
        <div class="s-row s-row--provider">
          <div class="s-row-info">
            <div class="s-row-label">
              {provider.name ?? provider.id}
            </div>
            <div class="s-row-desc">
              {describeProvider(provider, t)}
            </div>
          </div>
          <div class="s-row-control">
            <div class="s-row-actions s-row-actions--provider">
              <span class={`chip ${providerStatusClass(provider)}`}
                >{providerStatusLabel(provider, t)}</span
              >
            </div>
          </div>
        </div>

        {#if provider.connections?.length > 0}
          <div class="s-provider-connections">
            {#each provider.connections as connection (connection.id)}
              <div class="s-provider-connection-row">
                <div class="s-row-info">
                  <div class="s-provider-connection-label">
                    {connection.label ?? connection.id}
                  </div>
                  <div class="s-row-desc">
                    {isOAuthDeviceFlowConnection(connection)
                      ? t(
                          'settings.providers.oauthDescription',
                          'OAuth device authorization managed by the provider.',
                        )
                      : isOAuthConnection(connection)
                        ? t(
                            'settings.providers.oauthTokenDescription',
                            'OAuth token configured from environment or data directory.',
                          )
                        : t(
                            'settings.providers.apiKeyDescription',
                            'Static credential configured from environment or data directory.',
                          )}
                  </div>
                </div>

                <div class="s-row-control">
                  {#if isOAuthDeviceFlowConnection(connection)}
                    {@const state = getOAuthState(connection.id)}
                    {@const status = oauthStatus(connection)}
                    <div class="s-row-actions s-row-actions--provider">
                      {#if status === 'pending'}
                        <span class="s-inline-waiting">
                          <span class="s-inline-spinner" aria-hidden="true"
                          ></span>
                          {t(
                            'settings.providers.device_flow.waiting',
                            'Waiting for {provider} authorization…',
                            providerTranslationValues(provider),
                          )}
                        </span>
                        <button
                          class="btn-outline"
                          type="button"
                          onclick={() => cancelOAuthFlow(provider, connection)}
                        >
                          {t('settings.providers.device_flow.cancel', 'Cancel')}
                        </button>
                      {:else if status === 'connected'}
                        <span class="chip chip-green">
                          {t('settings.providers.connected', 'Connected')}
                        </span>
                        <button
                          class="btn-outline"
                          type="button"
                          onclick={() =>
                            disconnectOAuthProvider(provider, connection)}
                        >
                          {t('settings.providers.disconnect', 'Disconnect')}
                        </button>
                      {:else}
                        <button
                          class="btn-primary"
                          type="button"
                          onclick={() =>
                            startOAuthConnect(provider, connection)}
                        >
                          {t('settings.providers.connect', 'Connect')}
                        </button>
                      {/if}
                    </div>

                    {#if state.showDialog && state.dialogData}
                      <div
                        class="device-flow-inline"
                        role="dialog"
                        aria-modal="false"
                        aria-labelledby={`device-flow-title-${connection.id}`}
                      >
                        <div class="device-flow-header">
                          <p class="device-flow-eyebrow">
                            {t(
                              'settings.providers.device_flow.eyebrow',
                              'OAuth',
                            )}
                          </p>
                          <h3 id={`device-flow-title-${connection.id}`}>
                            {t(
                              'settings.providers.device_flow.title',
                              'Connect {provider}',
                              providerTranslationValues(provider),
                            )}
                          </h3>
                        </div>
                        <p class="device-flow-instructions">
                          {t(
                            'settings.providers.device_flow.instructions',
                            'Enter this code at the link below:',
                          )}
                        </p>
                        <div class="device-flow-code-row">
                          <code class="device-flow-code"
                            >{state.dialogData.user_code}</code
                          >
                          <button
                            class="btn-outline device-flow-copy"
                            type="button"
                            aria-label={t(
                              'settings.providers.device_flow.copy_aria',
                              'Copy device code {code}',
                              { code: state.dialogData.user_code },
                            )}
                            onclick={() =>
                              copyDeviceFlowUserCode(
                                connection,
                                state.dialogData.user_code,
                              )}
                          >
                            {copiedDeviceFlowConnectionId === connection.id
                              ? t(
                                  'settings.providers.device_flow.copied',
                                  'Copied',
                                )
                              : t('common.copy', 'Copy')}
                          </button>
                        </div>
                        <a
                          class="device-flow-link"
                          href={state.dialogData.verification_uri}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {state.dialogData.verification_uri}
                        </a>
                        <div class="device-flow-waiting" aria-live="polite">
                          <span class="s-inline-spinner" aria-hidden="true"
                          ></span>
                          <span>
                            {t(
                              'settings.providers.device_flow.waiting',
                              'Waiting for {provider} authorization…',
                              providerTranslationValues(provider),
                            )}
                          </span>
                        </div>
                        <div class="device-flow-actions">
                          <button
                            class="btn-outline"
                            type="button"
                            onclick={() =>
                              cancelOAuthFlow(provider, connection)}
                          >
                            {t(
                              'settings.providers.device_flow.cancel',
                              'Cancel',
                            )}
                          </button>
                        </div>
                      </div>
                    {/if}
                  {:else}
                    <span
                      class={`chip ${isConnectionConfigured(connection) ? 'chip-green' : 'chip-amber'}`}
                    >
                      {isConnectionConfigured(connection)
                        ? t(
                            'settings.providers.status.configured',
                            'Configured',
                          )
                        : t(
                            'settings.providers.status.missingCredentials',
                            'Missing credentials',
                          )}
                    </span>
                  {/if}
                </div>
              </div>
            {/each}
          </div>
        {/if}
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
{/if}
