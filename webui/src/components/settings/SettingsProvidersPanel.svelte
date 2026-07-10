<script>
  import ProviderConnectModal from './ProviderConnectModal.svelte';
  import Button from '../ui/Button.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    accountDisplayName,
    connectionReachability,
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
    isConnectionEnabled,
    isKeylessConnection,
    isOAuthAccount,
    isOAuthConnection,
    isOAuthDeviceFlowConnection,
    isProcessEnvAccount,
  } from '$lib/settingsView.js';
  import {
    SURFACE_FORM,
    shouldApplyReloadNow,
  } from '$lib/resourceInvalidation.js';

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

  export function handleProviderAuthCompleted(event) {
    forwardedAuthEvent = event;
  }

  const LOCAL_CONTEXT_DEFAULT_CAP = 32768;

  let refreshingModels = $state(false);
  let modalScope = $state(null);
  let forwardedAuthEvent = $state(null);
  // Flagged-local models (model.list → local: true), grouped per provider for
  // the "Local model context" editor inside that provider's card.
  let localModels = $state([]);
  // Draft input values for the context editor, keyed by full model id.
  let localContextDrafts = $state({});
  let localContextBusy = $state(false);
  // A provider change elsewhere is mirrored here through a settings reload, but
  // held while the key-input modal is open so a live edit is never interrupted.
  let pendingSettingsReload = $state(false);
  let lastModelsRefreshToken = null;

  let providerItems = $derived(getProviderItems(settings));
  let connectedProviders = $derived(getConnectedProviderItems(settings));
  let localContextWindows = $derived(
    settings?.local_models?.context_windows ?? {},
  );
  let localModelsByProvider = $derived(groupLocalModelsByProvider(localModels));
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
      void (modelsRefreshToken, loadLocalModels());
    }
  });

  async function loadLocalModels() {
    try {
      const result = await rpc('model.list', {});
      localModels = (result?.models ?? []).filter(
        (model) => model?.local === true,
      );
    } catch {
      localModels = [];
    }
  }

  function groupLocalModelsByProvider(models) {
    const grouped = {};
    for (const model of models) {
      if (!model?.provider_id) {
        continue;
      }
      if (!grouped[model.provider_id]) {
        grouped[model.provider_id] = [];
      }
      grouped[model.provider_id].push(model);
    }
    return grouped;
  }

  function localContextPlaceholder(model) {
    const effective =
      model?.effective_context_window ??
      Math.min(
        LOCAL_CONTEXT_DEFAULT_CAP,
        model?.context_window ?? LOCAL_CONTEXT_DEFAULT_CAP,
      );
    return String(effective);
  }

  function localContextDraftValue(model) {
    if (model.id in localContextDrafts) {
      return localContextDrafts[model.id];
    }
    const configured = localContextWindows[model.id];
    return configured === undefined || configured === null
      ? ''
      : String(configured);
  }

  async function saveLocalContextWindow(model, rawValue) {
    const trimmed = String(rawValue ?? '').trim();
    let value = null;
    if (trimmed !== '') {
      const parsed = Number(trimmed);
      if (!Number.isInteger(parsed) || parsed <= 0) {
        onToast({
          title: t(
            'settings.providers.localContext.invalidValue',
            'Context window must be a positive whole number',
          ),
          variant: 'error',
        });
        return;
      }
      value = parsed;
    }

    localContextBusy = true;
    localContextDrafts = { ...localContextDrafts, [model.id]: trimmed };
    try {
      await rpc('settings.update', {
        local_models: { context_windows: { [model.id]: value } },
      });
      onError('');
      await onReloadSettings();
      await loadLocalModels();
      localContextDrafts = {};
    } catch (error) {
      onToast({
        title: error?.message || String(error),
        variant: 'error',
      });
    } finally {
      localContextBusy = false;
    }
  }

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
      const result = await rpc('connection.set_enabled', {
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
    onError('');

    try {
      const result = await rpc('model.refresh_db');
      applyProviderRefreshResult(result);
      await rpc('model.list');
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
  <div class="s-providers-toolbar">
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
                    {:else if getConnectionAccounts(connection).length === 0 || isKeylessConnection(connection)}
                      <StatusChip variant="success">
                        {t('settings.providers.connected', 'Connected')}
                      </StatusChip>
                    {/if}
                    {#if connectionSupportsAddAccount(connection)}
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

              {#if isConnectionEnabled(connection) && getConnectionAccounts(connection).length > 0 && !isKeylessConnection(connection)}
                <ul class="s-connection-accounts">
                  {#each getConnectionAccounts(connection) as account (account.id)}
                    <li class="s-connection-account-row">
                      <span class="s-connection-account-id">
                        {accountDisplayName(account, t)}
                      </span>
                      <StatusChip
                        variant={isAccountUsable(account) ? 'success' : 'warn'}
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
                            {t('settings.providers.disconnect', 'Disconnect')}
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
                            {t('settings.providers.replaceKey', 'Replace key…')}
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
            </div>
          {/each}

          {#if getAddableConnections(provider).length > 0}
            <div class="s-provider-add-connection">
              <Button
                variant="secondary"
                onClick={() => openAddConnectionModal(provider)}
              >
                {t('settings.providers.add.connectionButton', 'Add connection')}
              </Button>
            </div>
          {/if}
        </div>

        {#if (localModelsByProvider[provider.id] ?? []).length > 0}
          <div class="s-provider-local-context">
            <div class="s-row-info">
              <div class="s-row-label">
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
            {#each localModelsByProvider[provider.id] as model (model.id)}
              <div class="s-local-context-row">
                <span class="s-local-context-model">{model.model_id}</span>
                <input
                  class="s-local-context-input"
                  type="number"
                  min="1024"
                  step="1024"
                  placeholder={localContextPlaceholder(model)}
                  value={localContextDraftValue(model)}
                  disabled={localContextBusy}
                  aria-label={t(
                    'settings.providers.localContext.inputLabel',
                    'Context window for {model}',
                    { model: model.model_id },
                  )}
                  onchange={(event) =>
                    saveLocalContextWindow(model, event.currentTarget.value)}
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
{/if}
