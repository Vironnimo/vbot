<script>
  import { onMount, tick } from 'svelte';

  import {
    getAgent,
    getSettings,
    listConnections,
    listModels,
    refreshModelDatabase as refreshModels,
    updateAgent,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    AGENT_FORM_MODE_EDIT,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import {
    buildModelSelectOptions,
    createModelCatalogLoader,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    parseModelSelectionValue,
  } from '$lib/modelSelection.js';
  import { getUsableProviderItems } from '$lib/settingsView.js';
  import {
    ONBOARDING_TARGET_AGENT_ID,
    connectedProviderId,
  } from '$lib/onboarding.js';

  import ProviderConnectModal from './settings/ProviderConnectModal.svelte';
  import OnboardingProviders from './OnboardingProviders.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import '../styles/onboarding.css';

  const noop = () => {};
  let {
    targetAgentId = ONBOARDING_TARGET_AGENT_ID,
    providerAuthEvent = null,
    modelsRefreshToken = 0,
    onComplete = noop,
    onDismiss = noop,
    onToast = noop,
  } = $props();

  const catalog = createModelCatalogLoader({ listModels, listConnections });
  let disposed = false;
  let settingsRequest = 0;
  let modelRequest = 0;
  let lastModelsRefreshToken = null;
  let settings = $state(null);
  let loadingSettings = $state(true);
  let settingsError = $state('');
  let step = $state('service');
  let stepHeading = $state();
  let modalScope = $state(null);
  let connectionRevision = $state(0);
  let preferredProviderId = $state('');
  let models = $state([]);
  let connections = $state([]);
  let loadingModels = $state(false);
  let refreshingModels = $state(false);
  let modelsError = $state('');
  let selectedModelValue = $state('');
  let showAllModels = $state(false);
  let onlyFree = $state(false);
  let assigning = $state(false);
  let assignError = $state('');

  let usableProviders = $derived(getUsableProviderItems(settings));
  let connectedProvider = $derived(
    connectedProviderId(settings, preferredProviderId),
  );
  let activeProvider = $derived(
    usableProviders.find((provider) => provider.id === connectedProvider),
  );
  let providerModels = $derived(
    models.filter((model) => model.provider_id === connectedProvider),
  );
  let allModelOptions = $derived(
    buildModelSelectOptions({
      models: providerModels,
      connections: connections.filter(
        (connection) => connection.provider_id === connectedProvider,
      ),
      translate: t,
    })
      .filter((option) => option.value)
      .map((option) => {
        const model = providerModels.find(
          (item) => item.id === parseModelSelectionValue(option.value).model,
        );
        return {
          ...option,
          label: model?.name
            ? option.label.replace(model.id, model.name)
            : option.label,
          secondaryLabel: [model?.model_id, option.secondaryLabel]
            .filter(Boolean)
            .join(' · '),
          searchText: `${model?.name ?? ''} ${option.label} ${option.secondaryLabel ?? ''}`,
        };
      }),
  );
  let suitableOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue,
    }),
  );
  let modelOptions = $derived(
    onlyFree
      ? suitableOptions.filter((option) =>
          parseModelSelectionValue(option.value).model.endsWith(':free'),
        )
      : suitableOptions,
  );
  let selectedOption = $derived(
    allModelOptions.find((option) => option.value === selectedModelValue),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - suitableOptions.length,
      translate: t,
    }),
  );
  let canStart = $derived(
    Boolean(selectedOption) &&
      !loadingModels &&
      !modelsError &&
      !settingsError &&
      !loadingSettings &&
      !assigning,
  );

  onMount(() => {
    void loadSettings();
    return () => {
      disposed = true;
      settingsRequest += 1;
      modelRequest += 1;
      catalog.invalidate();
    };
  });

  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
    } else if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void reflectResourceChange();
    }
  });

  async function loadSettings() {
    const request = ++settingsRequest;
    loadingSettings = true;
    settingsError = '';
    try {
      const result = await getSettings();
      if (disposed || request !== settingsRequest) return null;
      settings = result;
      return result;
    } catch (error) {
      if (!disposed && request === settingsRequest) {
        settingsError = `${t('onboarding.service.loadError', 'Providers could not be loaded.')} ${error.message}`;
      }
      return null;
    } finally {
      if (!disposed && request === settingsRequest) loadingSettings = false;
    }
  }

  async function reflectResourceChange() {
    const result = await loadSettings();
    if (!result || modalScope) return;
    if (step === 'model') void loadModels();
  }

  function providerName(provider) {
    return (
      provider?.name ?? provider?.id ?? t('onboarding.provider', 'Provider')
    );
  }

  async function focusStep() {
    await tick();
    if (!disposed) stepHeading?.focus();
  }

  function goToServiceStep() {
    step = 'service';
    void focusStep();
  }

  function selectModelProvider(providerId) {
    if (providerId !== preferredProviderId) {
      selectedModelValue = '';
      onlyFree = false;
      showAllModels = false;
      assignError = '';
    }
    preferredProviderId = providerId;
  }

  function goToModelStep() {
    if (disposed || !connectedProvider || loadingSettings || settingsError)
      return;
    selectModelProvider(connectedProvider);
    step = 'model';
    void loadModels();
    void focusStep();
  }

  async function handleConnected() {
    const providerId = modalScope?.scopedProvider?.id;
    await loadSettings();
    if (disposed) return;
    if (!preferredProviderId) preferredProviderId = providerId ?? '';
    modalScope = null;
    connectionRevision += 1;
    void refreshModelDatabase();
  }

  async function loadModels() {
    const request = ++modelRequest;
    loadingModels = true;
    modelsError = '';
    try {
      const result = await catalog.load();
      if (!result || disposed) return;
      models = result.models;
      connections = result.connections;
      if (
        !allModelOptions.some((option) => option.value === selectedModelValue)
      )
        selectedModelValue = '';
    } catch (error) {
      if (!disposed)
        modelsError = `${t('onboarding.model.loadError', 'Models could not be loaded.')} ${error.message}`;
    } finally {
      if (!disposed && request === modelRequest) loadingModels = false;
    }
  }

  async function refreshModelDatabase() {
    if (refreshingModels || disposed) return;
    refreshingModels = true;
    try {
      await refreshModels();
    } catch {
      // The bundled catalog is still useful when remote discovery fails.
    }
    if (!disposed) {
      await loadModels();
      refreshingModels = false;
    }
  }

  async function retryModels() {
    await loadSettings();
    if (!disposed) void refreshModelDatabase();
  }

  async function startChatting() {
    if (!canStart) return;
    const model = selectedModelValue;
    const agentId = targetAgentId;
    assigning = true;
    assignError = '';
    try {
      const current = await getAgent(agentId);
      if (disposed) return;
      const baseline = createAgentFormValues(current);
      const result = normalizeAgentForm(
        { ...baseline, model },
        { mode: AGENT_FORM_MODE_EDIT, initialValues: baseline },
      );
      if (!result.isValid) {
        assignError = t(
          'onboarding.model.assignError',
          'The Model could not be assigned.',
        );
        return;
      }
      await updateAgent(result.payload);
      if (!disposed) onComplete();
    } catch (error) {
      if (!disposed)
        assignError = `${t('onboarding.model.assignError', 'The Model could not be assigned.')} ${error.message}`;
    } finally {
      if (!disposed) assigning = false;
    }
  }
</script>

{#snippet arrow()}
  <svg
    viewBox="0 0 24 24"
    width="18"
    height="18"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5" /></svg
  >
{/snippet}
{#snippet check()}
  <svg
    viewBox="0 0 24 24"
    width="18"
    height="18"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg
  >
{/snippet}

<section class="view onboarding-view active" aria-labelledby="onboarding-title">
  <div class="onboarding-scroll">
    <div class="onboarding-layout">
      <header class="onboarding-overview">
        <div class="onboarding-brand">
          <img
            src="/brand/vbot-mark-transparent.png"
            alt=""
            width="44"
            height="32"
          />
          <h2 id="onboarding-title">{t('onboarding.title', 'Set up vBot')}</h2>
        </div>
        <Button variant="tertiary" disabled={assigning} onClick={onDismiss}>
          {t('onboarding.dismiss', 'Explore first')}
        </Button>
      </header>

      <nav aria-label={t('onboarding.progress', 'Setup progress')}>
        <ol class="onboarding-progress">
          <li
            class:current={step === 'service'}
            class:complete={Boolean(connectedProvider)}
          >
            <button
              type="button"
              aria-current={step === 'service' ? 'step' : undefined}
              disabled={assigning}
              onclick={goToServiceStep}
            >
              <span class="onboarding-step-number" aria-hidden="true"
                >{#if connectedProvider}{@render check()}{:else}1{/if}</span
              >
              <span>{t('onboarding.progress.service', 'Providers')}</span>
            </button>
          </li>
          <li class:current={step === 'model'}>
            <button
              type="button"
              aria-current={step === 'model' ? 'step' : undefined}
              disabled={!connectedProvider ||
                assigning ||
                loadingSettings ||
                Boolean(settingsError)}
              onclick={goToModelStep}
            >
              <span class="onboarding-step-number" aria-hidden="true">2</span>
              <span>{t('onboarding.progress.model', 'First Model')}</span>
            </button>
          </li>
        </ol>
      </nav>

      <div class="onboarding-panel">
        <header class="onboarding-step-header">
          <h3
            class="onboarding-step-title"
            bind:this={stepHeading}
            tabindex="-1"
          >
            {step === 'service'
              ? t('onboarding.step.service.title', 'Connect your Providers')
              : t('onboarding.step.model.title', 'Choose your first Model')}
          </h3>
          <p class="onboarding-step-subtitle">
            {step === 'service'
              ? t(
                  'onboarding.step.service.subtitle',
                  'Choose a Provider, then how to connect. Add as many as you use.',
                )
              : t(
                  'onboarding.step.model.subtitle',
                  'Choose the Model your Agent starts with. All your connected Providers remain available.',
                )}
          </p>
        </header>

        {#if settingsError}
          <Banner variant="error" role="alert">
            {settingsError}
            <Button
              variant="secondary"
              disabled={loadingSettings}
              onClick={step === 'service' ? loadSettings : retryModels}
              >{t('onboarding.retry', 'Try again')}</Button
            >
          </Banner>
        {/if}

        {#if step === 'service'}
          {#if loadingSettings && !settings}
            <p class="onboarding-loading" role="status">
              <span class="s-inline-spinner" aria-hidden="true"></span>{t(
                'onboarding.service.loading',
                'Loading Providers…',
              )}
            </p>
          {:else if settings}
            <OnboardingProviders
              {settings}
              disabled={loadingSettings || Boolean(settingsError)}
              resetToken={connectionRevision}
              onSelect={(scope) => (modalScope = scope)}
            />
          {/if}

          <div
            class="onboarding-connected-summary"
            aria-live="polite"
            aria-atomic="true"
          >
            {#if usableProviders.length}
              <p>
                <span class="onboarding-connected-icon">{@render check()}</span
                ><strong
                  >{t(
                    'onboarding.service.connectedCount',
                    '{count} connected',
                    { count: usableProviders.length },
                  )}</strong
                ><span
                  >{t(
                    'onboarding.service.addMore',
                    'Add another, or continue when you’re ready.',
                  )}</span
                >
              </p>
              <ul
                aria-label={t(
                  'onboarding.service.existing',
                  'Connected Providers',
                )}
              >
                {#each usableProviders as provider (provider.id)}
                  <li>{providerName(provider)}</li>
                {/each}
              </ul>
            {:else}
              <p>
                {t(
                  'onboarding.service.required',
                  'Connect at least one Provider to continue.',
                )}
              </p>
            {/if}
          </div>
          <footer class="onboarding-footer">
            <p class="onboarding-footnote">
              {t(
                'onboarding.service.later',
                'You can add or change Providers later in Settings.',
              )}
            </p>
            <Button
              variant="primary"
              class="onboarding-continue"
              disabled={!connectedProvider ||
                loadingSettings ||
                Boolean(settingsError)}
              onClick={goToModelStep}
            >
              {t(
                'onboarding.service.continue',
                'Continue to Model',
              )}{@render arrow()}
            </Button>
          </footer>
        {:else}
          <div class="onboarding-model-provider">
            {#if usableProviders.length > 1}
              <label for="onboarding-model-provider"
                >{t('onboarding.provider', 'Provider')}</label
              >
              <SearchableDropdown
                id="onboarding-model-provider"
                panelClass="onboarding-model-menu"
                value={connectedProvider}
                options={usableProviders.map((provider) => ({
                  value: provider.id,
                  label: providerName(provider),
                }))}
                disabled={assigning}
                ariaLabel={t('onboarding.provider', 'Provider')}
                searchPlaceholder={t(
                  'onboarding.service.search',
                  'Search Providers…',
                )}
                onValueChange={selectModelProvider}
              />
            {:else if activeProvider}
              <div class="onboarding-connection-summary">
                <span class="onboarding-connected-icon">{@render check()}</span
                ><strong>{providerName(activeProvider)}</strong><span
                  >{t('onboarding.service.connected', 'Connected')}</span
                >
              </div>
            {:else}
              <p class="onboarding-help">
                {t(
                  'onboarding.model.disconnected',
                  'This Provider is no longer connected. Go back to Providers to reconnect.',
                )}
              </p>
            {/if}
          </div>
          <div
            class="onboarding-model-body"
            aria-busy={loadingModels || refreshingModels}
          >
            {#if (loadingModels || refreshingModels) && !allModelOptions.length}
              <p class="onboarding-loading" role="status">
                <span class="s-inline-spinner" aria-hidden="true"></span>{t(
                  'onboarding.model.loading',
                  'Finding available Models…',
                )}
              </p>
            {:else if modelsError}
              <Banner variant="error" role="alert">{modelsError}</Banner>
              <Button variant="secondary" onClick={retryModels}
                >{t('onboarding.retry', 'Try again')}</Button
              >
            {:else if !allModelOptions.length}
              <EmptyState
                density="compact"
                title={t(
                  'onboarding.model.emptyTitle',
                  'No Models available yet',
                )}
                description={t(
                  'onboarding.model.empty',
                  'Check that your Provider has Models available. For a local Provider, start its server and install a Model, then try again.',
                )}
              >
                {#snippet actions()}<Button
                    variant="secondary"
                    onClick={retryModels}
                    >{t('onboarding.retry', 'Try again')}</Button
                  >{/snippet}
              </EmptyState>
            {:else}
              <div class="onboarding-model-label">
                <label for="onboarding-model"
                  >{t('onboarding.model.label', 'Model')}</label
                >{#if connectedProvider === 'openrouter'}<Button
                    variant="secondary"
                    class="onboarding-free-filter"
                    aria-pressed={onlyFree}
                    disabled={assigning}
                    onClick={() => {
                      onlyFree = !onlyFree;
                      if (
                        onlyFree &&
                        !parseModelSelectionValue(
                          selectedModelValue,
                        ).model.endsWith(':free')
                      ) {
                        selectedModelValue = '';
                      }
                    }}
                    >{t(
                      'onboarding.model.freeOnly',
                      'Free Models only',
                    )}</Button
                  >{/if}
              </div>
              <SearchableDropdown
                id="onboarding-model"
                panelClass="onboarding-model-menu"
                value={selectedModelValue}
                options={modelOptions}
                disabled={assigning}
                placeholder={t('onboarding.model.placeholder', 'Find a Model…')}
                searchPlaceholder={t(
                  'onboarding.model.searchPlaceholder',
                  'Search by name…',
                )}
                emptyLabel={onlyFree
                  ? t(
                      'onboarding.model.noFree',
                      'No free Models match. Turn off the free filter to see more.',
                    )
                  : t(
                      'onboarding.model.searchEmpty',
                      'No Models match. Try another search or show all Models below.',
                    )}
                ariaLabel={t('onboarding.model.label', 'Model')}
                ariaDescribedby="onboarding-model-help"
                footerActionLabel={modelFilterFooter}
                onFooterAction={() => (showAllModels = !showAllModels)}
                onValueChange={(value) => {
                  selectedModelValue = value;
                  assignError = '';
                }}
              />
              <p id="onboarding-model-help" class="onboarding-help">
                {t(
                  'onboarding.model.help',
                  'Models suited to Agent work are shown first. Search by name, or use “Show all Models” in the list to see the rest.',
                )}
              </p>
              {#if selectedOption}
                <div class="onboarding-selection" role="status">
                  <span class="onboarding-selection-icon"
                    >{@render check()}</span
                  >
                  <div>
                    <strong>{selectedOption.label}</strong>
                    <p>
                      {t(
                        'onboarding.model.ready',
                        'Start chatting to save this Model for your Agent. You can change it later in Agents.',
                      )}
                    </p>
                  </div>
                </div>
              {:else}
                <p class="onboarding-model-prompt">
                  {t(
                    'onboarding.model.prompt',
                    'Choose a Model above to continue.',
                  )}
                </p>
              {/if}
            {/if}
            {#if assignError}<Banner variant="error" role="alert"
                >{assignError}</Banner
              >{/if}
          </div>

          <footer class="onboarding-footer">
            <Button
              variant="tertiary"
              disabled={assigning}
              onClick={goToServiceStep}
              >{t('onboarding.model.back', 'Back to Providers')}</Button
            >
            <Button
              variant="primary"
              disabled={!canStart}
              loading={assigning}
              onClick={startChatting}
              >{assigning
                ? t('common.saving', 'Saving…')
                : t(
                    'onboarding.model.start',
                    'Start chatting',
                  )}{@render arrow()}</Button
            >
          </footer>
        {/if}
      </div>
    </div>
  </div>

  {#if modalScope}
    <ProviderConnectModal
      providers={modalScope.providers}
      scopedProvider={modalScope.scopedProvider}
      scopedConnection={modalScope.scopedConnection}
      setupMode={true}
      {providerAuthEvent}
      {onToast}
      onCompleted={handleConnected}
      onClose={() => (modalScope = null)}
    />
  {/if}
</section>
