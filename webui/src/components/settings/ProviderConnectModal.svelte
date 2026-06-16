<script>
  import { untrack } from 'svelte';
  import Button from '../ui/Button.svelte';
  import Modal from '../ui/Modal.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    DEFAULT_ACCOUNT_ID,
    deriveAccountCredentialKey,
    getAddableConnections,
    getPublicConnectionId,
    isValidAccountId,
    normalizeAccountId,
  } from '$lib/settingsView.js';

  const noop = () => {};

  let {
    providers = [],
    scopedProvider = null,
    scopedConnection = null,
    scopedAccount = null,
    providerAuthEvent = null,
    connectProvider = null,
    disconnectProvider = null,
    onToast = noop,
    onCompleted = noop,
    onClose = noop,
  } = $props();

  function initialConnection() {
    if (scopedConnection) {
      return scopedConnection;
    }
    if (scopedProvider) {
      const addable = getAddableConnections(scopedProvider);
      if (addable.length === 1) {
        return addable[0];
      }
    }
    return null;
  }

  // Seeded once from the props at mount (untrack avoids a reactive read that
  // would only capture the initial value anyway — the modal is recreated per open).
  let selectedProvider = $state(untrack(() => scopedProvider) ?? null);
  let selectedConnection = $state(initialConnection());
  let apiKeyValue = $state('');
  let accountValue = $state(untrack(() => scopedAccount) ?? '');
  let saving = $state(false);
  let errorMessage = $state('');
  let oauthData = $state(null);
  let oauthActive = $state(false);
  let activeOAuthAccount = $state(DEFAULT_ACCOUNT_ID);
  let copiedCode = $state(false);
  let handledAuthEvent = null;

  let accountFixed = $derived(scopedAccount !== null);
  let accountInvalid = $derived(
    accountValue.trim().length > 0 && !isValidAccountId(accountValue.trim()),
  );

  let step = $derived(
    !selectedProvider
      ? 'provider'
      : !selectedConnection
        ? 'method'
        : selectedConnection.type === 'api_key'
          ? 'api-key'
          : 'oauth',
  );
  let methodOptions = $derived(
    selectedProvider ? getAddableConnections(selectedProvider) : [],
  );
  let canGoBack = $derived(computeCanGoBack(step));

  $effect(() => {
    if (providerAuthEvent && providerAuthEvent !== handledAuthEvent) {
      handledAuthEvent = providerAuthEvent;
      handleAuthEvent(providerAuthEvent);
    }
  });

  function providerName(provider) {
    return provider?.name ?? provider?.id ?? 'Provider';
  }

  function providerValues(provider) {
    return { provider: providerName(provider) };
  }

  function computeCanGoBack(currentStep) {
    if (saving || oauthActive) {
      return false;
    }
    if (currentStep === 'method') {
      return !scopedProvider;
    }
    if (currentStep === 'api-key' || currentStep === 'oauth') {
      if (scopedConnection) {
        return false;
      }
      return methodOptions.length > 1 || !scopedProvider;
    }
    return false;
  }

  function chooseProvider(provider) {
    selectedProvider = provider;
    errorMessage = '';
    const addable = getAddableConnections(provider);
    if (addable.length === 1) {
      selectedConnection = addable[0];
    }
  }

  function chooseConnection(connection) {
    selectedConnection = connection;
    errorMessage = '';
  }

  function effectiveAccount() {
    return scopedAccount ?? normalizeAccountId(accountValue);
  }

  function goBack() {
    errorMessage = '';
    apiKeyValue = '';
    if (!accountFixed) {
      accountValue = '';
    }
    if (selectedConnection && methodOptions.length > 1) {
      selectedConnection = null;
      return;
    }
    selectedConnection = null;
    if (!scopedProvider) {
      selectedProvider = null;
    }
  }

  function close() {
    if (saving) {
      return;
    }
    if (oauthActive) {
      void cancelOAuthFlow();
    }
    onClose();
  }

  async function submitApiKey(event) {
    event.preventDefault();

    const value = apiKeyValue.trim();
    if (!value || saving || accountInvalid) {
      return;
    }

    saving = true;
    errorMessage = '';

    try {
      await rpc('provider.set_key', {
        provider_id: selectedProvider.id,
        connection_id: getPublicConnectionId(selectedConnection),
        account: effectiveAccount(),
        value,
      });
      onToast({
        title: t(
          'settings.providers.device_flow.success_toast',
          '{provider} connected successfully',
          providerValues(selectedProvider),
        ),
        variant: 'success',
      });
      await onCompleted();
      onClose();
    } catch (error) {
      errorMessage = `${t('settings.providers.add.keyError', 'API key could not be saved.')} ${error.message}`;
    } finally {
      saving = false;
    }
  }

  async function startOAuthFlow() {
    if (oauthActive || accountInvalid) {
      return;
    }

    errorMessage = '';
    copiedCode = false;
    oauthActive = true;
    activeOAuthAccount = effectiveAccount();
    oauthData = null;

    try {
      const response = await callConnectProvider(
        selectedProvider.id,
        getPublicConnectionId(selectedConnection),
        activeOAuthAccount,
      );
      oauthData = response?.user_code ? response : null;
    } catch (error) {
      oauthActive = false;
      errorMessage = `${t('settings.providers.connectError', 'Provider connection could not be started.')} ${error.message}`;
    }
  }

  async function cancelOAuthFlow() {
    try {
      await callDisconnectProvider(
        selectedProvider.id,
        getPublicConnectionId(selectedConnection),
        activeOAuthAccount,
      );
    } catch {
      // Cancelling a pending flow is best-effort; the dialog closes anyway.
    }
    oauthActive = false;
    oauthData = null;
    copiedCode = false;
  }

  function handleAuthEvent(event) {
    const payload = event.payload ?? event;
    if (
      !oauthActive ||
      !selectedProvider ||
      !selectedConnection ||
      payload.provider_id !== selectedProvider.id ||
      payload.connection_id !== getPublicConnectionId(selectedConnection) ||
      payload.account !== activeOAuthAccount
    ) {
      return;
    }

    if (payload.success === true) {
      completeOAuthFlow();
      return;
    }

    oauthActive = false;
    oauthData = null;
    errorMessage = t(
      'settings.providers.device_flow.error_toast',
      'Authorization failed or timed out',
    );
  }

  async function completeOAuthFlow() {
    oauthActive = false;
    oauthData = null;
    onToast({
      title: t(
        'settings.providers.device_flow.success_toast',
        '{provider} connected successfully',
        providerValues(selectedProvider),
      ),
      variant: 'success',
    });
    await onCompleted();
    onClose();
  }

  async function copyUserCode() {
    const userCode = oauthData?.user_code;
    if (!userCode) {
      return;
    }

    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
      onToast({
        title: t(
          'settings.providers.device_flow.copy_error',
          'Device code could not be copied.',
        ),
        variant: 'error',
      });
      return;
    }

    try {
      await navigator.clipboard.writeText(userCode);
      copiedCode = true;
      onToast({
        title: t(
          'settings.providers.device_flow.copy_success',
          'Device code copied.',
        ),
        variant: 'success',
      });
    } catch {
      onToast({
        title: t(
          'settings.providers.device_flow.copy_error',
          'Device code could not be copied.',
        ),
        variant: 'error',
      });
    }
  }

  async function callConnectProvider(providerId, connectionId, account) {
    if (typeof connectProvider === 'function') {
      return connectProvider(providerId, connectionId, account, { rpc });
    }

    return rpc('provider.connect', {
      provider_id: providerId,
      connection_id: connectionId,
      account,
    });
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

  function connectionMethodLabel(connection) {
    return connection.type === 'api_key'
      ? t('settings.providers.add.methodApiKey', 'API key')
      : t('settings.providers.add.methodOAuth', 'Sign in (OAuth)');
  }

  function connectionMethodDescription(connection) {
    return connection.type === 'api_key'
      ? t(
          'settings.providers.add.methodApiKeyDescription',
          'Paste a static API key; it is stored in the data directory.',
        )
      : t(
          'settings.providers.add.methodOAuthDescription',
          'Authorize vBot through the provider account in a browser.',
        );
  }
</script>

{#snippet accountField(fieldDisabled)}
  <label class="modal-field">
    <span class="modal-label">
      {t('settings.providers.accounts.nameLabel', 'Account')}
    </span>
    <input
      class="s-input"
      type="text"
      autocomplete="off"
      placeholder={DEFAULT_ACCOUNT_ID}
      value={accountValue}
      disabled={fieldDisabled || accountFixed}
      oninput={(event) => {
        accountValue = event.currentTarget.value;
        errorMessage = '';
      }}
    />
  </label>
  {#if accountInvalid}
    <p class="provider-connect-modal__error" role="alert">
      {t(
        'settings.providers.accounts.invalidId',
        'Account names use 1–32 lowercase letters, digits, or underscores and start with a letter or digit.',
      )}
    </p>
  {:else if !accountFixed}
    <p class="provider-connect-modal__hint">
      {t(
        'settings.providers.accounts.nameHint',
        'Optional name for this account. Only needed if you add more than one — otherwise leave it empty.',
      )}
    </p>
  {/if}
{/snippet}

<Modal
  title={selectedProvider
    ? t(
        'settings.providers.device_flow.title',
        'Connect {provider}',
        providerValues(selectedProvider),
      )
    : t('settings.providers.add.title', 'Add provider')}
  labelledById="provider-connect-modal-title"
  class="provider-connect-modal"
  closeDisabled={saving}
  onClose={close}
>
  {#snippet body()}
    <div class="modal-body provider-connect-modal__body">
      {#if step === 'provider'}
        {#if providers.length === 0}
          <p class="provider-connect-modal__hint">
            {t(
              'settings.providers.add.allConnected',
              'All available providers are already connected.',
            )}
          </p>
        {:else}
          <p class="provider-connect-modal__hint">
            {t(
              'settings.providers.add.chooseProvider',
              'Choose a provider to connect.',
            )}
          </p>
          <div class="provider-pick-list" role="list">
            {#each providers as provider (provider.id)}
              <div role="listitem">
                <button
                  type="button"
                  class="provider-pick-item"
                  onclick={() => chooseProvider(provider)}
                >
                  <span class="provider-pick-item__name">
                    {providerName(provider)}
                  </span>
                  {#if provider.base_url}
                    <span class="provider-pick-item__detail">
                      {provider.base_url}
                    </span>
                  {/if}
                </button>
              </div>
            {/each}
          </div>
        {/if}
      {:else if step === 'method'}
        <p class="provider-connect-modal__hint">
          {t(
            'settings.providers.add.chooseMethod',
            'Choose how to connect {provider}.',
            providerValues(selectedProvider),
          )}
        </p>
        <div class="provider-pick-list" role="list">
          {#each methodOptions as connection (connection.id)}
            <div role="listitem">
              <button
                type="button"
                class="provider-pick-item"
                onclick={() => chooseConnection(connection)}
              >
                <span class="provider-pick-item__name">
                  {connectionMethodLabel(connection)}
                  <span class="provider-pick-item__connection">
                    {connection.label ?? connection.id}
                  </span>
                </span>
                <span class="provider-pick-item__detail">
                  {connectionMethodDescription(connection)}
                </span>
              </button>
            </div>
          {/each}
        </div>
      {:else if step === 'api-key'}
        <form id="provider-connect-key-form" onsubmit={submitApiKey}>
          <label class="modal-field">
            <span class="modal-label">
              {t('settings.providers.add.apiKeyLabel', 'API key')}
            </span>
            <input
              class="s-input"
              type="password"
              autocomplete="off"
              placeholder={t(
                'settings.providers.add.apiKeyPlaceholder',
                'Paste the API key…',
              )}
              value={apiKeyValue}
              disabled={saving}
              oninput={(event) => {
                apiKeyValue = event.currentTarget.value;
                errorMessage = '';
              }}
            />
          </label>
          {@render accountField(saving)}
          {#if selectedConnection.credential_key}
            <p class="provider-connect-modal__hint">
              {t(
                'settings.providers.add.apiKeyHint',
                'Stored as {credentialKey} in the data directory .env.',
                {
                  credentialKey: deriveAccountCredentialKey(
                    selectedConnection.credential_key,
                    effectiveAccount(),
                  ),
                },
              )}
            </p>
          {/if}
        </form>
      {:else if step === 'oauth'}
        {#if oauthActive && oauthData}
          <p class="device-flow-instructions">
            {t(
              'settings.providers.device_flow.instructions',
              'Enter this code at the link below:',
            )}
          </p>
          <div class="device-flow-code-row">
            <code class="device-flow-code">{oauthData.user_code}</code>
            <Button
              variant="secondary"
              class="device-flow-copy"
              ariaLabel={t(
                'settings.providers.device_flow.copy_aria',
                'Copy device code {code}',
                { code: oauthData.user_code },
              )}
              onClick={copyUserCode}
            >
              {copiedCode
                ? t('settings.providers.device_flow.copied', 'Copied')
                : t('common.copy', 'Copy')}
            </Button>
          </div>
          <a
            class="device-flow-link"
            href={oauthData.verification_uri}
            target="_blank"
            rel="noreferrer"
          >
            {oauthData.verification_uri}
          </a>
          <div class="device-flow-waiting" aria-live="polite">
            <span class="s-inline-spinner" aria-hidden="true"></span>
            <span>
              {t(
                'settings.providers.device_flow.waiting',
                'Waiting for {provider} authorization…',
                providerValues(selectedProvider),
              )}
            </span>
          </div>
        {:else if oauthActive}
          <div class="device-flow-waiting" aria-live="polite">
            <span class="s-inline-spinner" aria-hidden="true"></span>
            <span>
              {t(
                'settings.providers.device_flow.waiting',
                'Waiting for {provider} authorization…',
                providerValues(selectedProvider),
              )}
            </span>
          </div>
        {:else}
          <p class="provider-connect-modal__hint">
            {t(
              'settings.providers.add.oauthIntro',
              'Click Connect to begin. vBot then shows a code to enter at {provider} in your browser.',
              providerValues(selectedProvider),
            )}
          </p>
          {@render accountField(false)}
        {/if}
      {/if}

      {#if errorMessage}
        <p class="provider-connect-modal__error" role="alert">
          {errorMessage}
        </p>
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    {#if canGoBack}
      <Button variant="secondary" disabled={saving} onClick={goBack}>
        {t('common.back', 'Back')}
      </Button>
    {/if}
    <Button variant="secondary" disabled={saving} onClick={close}>
      {t('common.cancel', 'Cancel')}
    </Button>
    {#if step === 'api-key'}
      <Button
        type="submit"
        form="provider-connect-key-form"
        variant="primary"
        disabled={saving || apiKeyValue.trim().length === 0 || accountInvalid}
      >
        {saving
          ? t('common.saving', 'Saving…')
          : t('settings.providers.add.saveKey', 'Save key')}
      </Button>
    {:else if step === 'oauth' && !oauthActive}
      <Button
        variant="primary"
        disabled={accountInvalid}
        onClick={startOAuthFlow}
      >
        {t('settings.providers.connect', 'Connect')}
      </Button>
    {/if}
  {/snippet}
</Modal>
