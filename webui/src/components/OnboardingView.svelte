<script>
  import { onMount, tick, untrack } from 'svelte';

  import {
    getAgent,
    getSettings,
    getSetup,
    updateSetup,
    finishSetup,
    listConnections,
    listModels,
    refreshModelDatabase as refreshModels,
    updateAgent,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import { useAutosaveContext } from '$lib/autosave.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
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
    CORE_ONBOARDING_STEPS,
    ONBOARDING_TARGET_AGENT_ID,
    connectedProviderId,
    pickDefaultOnboardingModel,
  } from '$lib/onboarding.js';

  import ProviderConnectModal from './settings/ProviderConnectModal.svelte';
  import OnboardingProviders from './OnboardingProviders.svelte';
  import OnboardingComponents from './OnboardingComponents.svelte';
  import OnboardingServices from './OnboardingServices.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import '../styles/onboarding.css';

  const noop = () => {};
  let {
    targetAgentId = ONBOARDING_TARGET_AGENT_ID,
    initialSetup = null,
    onSetupChange = noop,
    providerAuthEvent = null,
    modelsRefreshToken = 0,
    onComplete = noop,
    onDismiss = noop,
    onToast = noop,
  } = $props();

  const catalog = createModelCatalogLoader({ listModels, listConnections });
  let disposed = false;
  let settingsRequest = 0;
  let setupRevision = 0;
  let modelRequest = 0;
  let lastModelsRefreshToken = null;
  let settings = $state(null);
  let loadingSettings = $state(true);
  let settingsError = $state('');
  let step = $state('provider');
  let activeAgentId = $state('');
  let setupError = $state('');
  let transitioning = $state(false);
  let observedSetup = null;
  let pendingSharedSetup = $state(null);
  let fieldFocused = $state(false);
  const autosave = useAutosaveContext();
  const coreStepIds = CORE_ONBOARDING_STEPS;
  const stepIds = ['provider', 'model', 'components', 'services'];
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
  let savedAgentModel = $state('');
  let userClearedModel = $state(false);
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
          labelLead: model?.name ? '' : option.labelLead,
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
    }),
  );
  let canStart = $derived(
    Boolean(selectedOption) &&
      !loadingModels &&
      !modelsError &&
      !settingsError &&
      !loadingSettings &&
      !assigning &&
      !transitioning,
  );

  onMount(() => {
    void loadSettings();
    if (!initialSetup) void loadInitialSetup();
    return () => {
      disposed = true;
      settingsRequest += 1;
      modelRequest += 1;
      catalog.invalidate();
    };
  });

  $effect(() => {
    const shared = initialSetup;
    if (!shared || shared === observedSetup) return;
    observedSetup = shared;
    setupRevision += 1;
    untrack(() => {
      pendingSharedSetup = shared;
    });
  });

  $effect(() => {
    if (
      !pendingSharedSetup ||
      transitioning ||
      assigning ||
      modalScope ||
      fieldFocused
    )
      return;
    const shared = pendingSharedSetup;
    pendingSharedSetup = null;
    untrack(() => {
      if (stepIds.includes(shared.step) && shared.step !== step) {
        step = shared.step;
        if (step === 'model') void loadModels();
      }
      activeAgentId = shared.agent_id || targetAgentId;
    });
  });

  function reflectFocus() {
    queueMicrotask(() => {
      if (!disposed)
        fieldFocused = Boolean(
          document.activeElement?.matches(
            'input, textarea, select, [role="combobox"]',
          ),
        );
    });
  }

  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
    } else if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void reflectResourceChange();
    }
  });

  async function loadInitialSetup() {
    const request = setupRevision;
    const isCurrent = () =>
      !disposed && !initialSetup && request === setupRevision;
    try {
      const state = await getSetup();
      if (isCurrent()) pendingSharedSetup = state;
    } catch (error) {
      if (isCurrent()) setupError = error.message;
    }
  }

  function stepLabel(id) {
    if (id === 'provider') return t('onboarding.progress.service');
    return t('onboarding.progress.model');
  }

  function isStepComplete(id) {
    if (id === 'provider') {
      return usableProviders.length > 0 && step !== 'provider';
    }
    if (id === 'model') {
      return (
        Boolean(selectedOption) &&
        (step === 'components' || step === 'services')
      );
    }
    return false;
  }

  function stepTitle() {
    if (step === 'provider') return t('onboarding.step.service.title');
    if (step === 'model') return t('onboarding.step.model.title');
    if (step === 'components') return t('onboarding.step.components.title');
    return t('onboarding.step.services.title');
  }

  function stepDescription() {
    if (step === 'provider') return t('onboarding.step.service.subtitle');
    if (step === 'model') return t('onboarding.step.model.subtitle');
    if (step === 'components') return t('onboarding.step.components.subtitle');
    return t('onboarding.step.services.subtitle');
  }

  async function moveToStep(next) {
    if (transitioning || assigning || disposed) return false;
    return autosave.requestTransition(async () => {
      transitioning = true;
      setupError = '';
      try {
        if (
          step === 'model' &&
          next !== 'provider' &&
          selectedOption &&
          !(await saveModel())
        )
          return false;
        const result = await updateSetup(next);
        if (disposed) return false;
        setupRevision += 1;
        pendingSharedSetup = null;
        activeAgentId = result.agent_id || targetAgentId;
        onSetupChange(result);
        step = next;
        if (next === 'model') void loadModels();
        void focusStep();
        return true;
      } catch (error) {
        if (!disposed) setupError = error.message;
        return false;
      } finally {
        if (!disposed) transitioning = false;
      }
    });
  }

  async function finish(callback = onDismiss, { saveSelection = true } = {}) {
    if (transitioning || assigning || disposed) return;
    return autosave.requestTransition(async () => {
      transitioning = true;
      setupError = '';
      try {
        if (
          saveSelection &&
          step === 'model' &&
          selectedOption &&
          !(await saveModel())
        )
          return false;
        const result = await finishSetup();
        if (disposed) return false;
        setupRevision += 1;
        pendingSharedSetup = null;
        onSetupChange(result);
        callback();
        return true;
      } catch (error) {
        if (!disposed) setupError = error.message;
        return false;
      } finally {
        if (!disposed) transitioning = false;
      }
    });
  }

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
        settingsError = `${t('onboarding.service.loadError')} ${error.message}`;
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
    return provider?.name ?? provider?.id ?? t('onboarding.provider');
  }

  async function focusStep() {
    await tick();
    if (!disposed) stepHeading?.focus();
  }

  function goToServiceStep() {
    void moveToStep('provider');
  }

  // The wizard covers every view: Back returns to its first step, and from
  // there sets the wizard aside like dismissing it.
  const navigation = useNavigation();
  $effect(() =>
    navigation?.registerLayer({
      close: () =>
        step === 'provider'
          ? finish()
          : moveToStep(stepIds[stepIds.indexOf(step) - 1]),
    }),
  );

  function selectModelProvider(providerId) {
    if (providerId !== preferredProviderId) {
      selectedModelValue = '';
      userClearedModel = false;
      onlyFree = false;
      showAllModels = false;
      assignError = '';
    }
    preferredProviderId = providerId;
    if (step === 'model' && !selectedModelValue && !userClearedModel) {
      selectedModelValue = pickDefaultOnboardingModel(suitableOptions, {
        currentModel: savedAgentModel,
      });
    }
  }

  function goToModelStep() {
    if (disposed || !connectedProvider || loadingSettings || settingsError)
      return;
    selectModelProvider(connectedProvider);
    void moveToStep('model');
  }

  async function handleConnected() {
    const hadConnectedBefore = usableProviders.length > 0;
    const providerId = modalScope?.scopedProvider?.id ?? '';
    await loadSettings();
    if (disposed) return;
    if (providerId) preferredProviderId = providerId;
    modalScope = null;
    connectionRevision += 1;
    void refreshModelDatabase();
    if (!hadConnectedBefore && connectedProviderId(settings, providerId)) {
      void moveToStep('model');
    }
  }

  async function loadModels() {
    const request = ++modelRequest;
    loadingModels = true;
    modelsError = '';
    userClearedModel = false;
    try {
      const agentId = activeAgentId || targetAgentId;
      const [result, agent] = await Promise.all([
        catalog.load(),
        getAgent(agentId).catch(() => null),
      ]);
      if (!result || disposed) return;
      models = result.models;
      connections = result.connections;
      if (typeof agent?.model === 'string') {
        savedAgentModel = agent.model;
      }
      if (
        !allModelOptions.some((option) => option.value === selectedModelValue)
      ) {
        selectedModelValue = '';
      }
      if (!selectedModelValue && !onlyFree) {
        selectedModelValue = pickDefaultOnboardingModel(suitableOptions, {
          currentModel: savedAgentModel,
        });
      }
    } catch (error) {
      if (!disposed)
        modelsError = `${t('onboarding.model.loadError')} ${error.message}`;
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

  async function saveModel() {
    if (!selectedOption) return false;
    const model = selectedModelValue;
    const agentId = activeAgentId || targetAgentId;
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
        assignError = t('onboarding.model.assignError');
        return false;
      }
      await updateAgent(result.payload);
      return !disposed;
    } catch (error) {
      if (!disposed)
        assignError = `${t('onboarding.model.assignError')} ${error.message}`;
      return false;
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

<section
  class="view onboarding-view active"
  aria-labelledby="onboarding-title"
  onfocusin={reflectFocus}
  onfocusout={reflectFocus}
>
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
          <h2 id="onboarding-title">{t('onboarding.title')}</h2>
        </div>
        <Button
          variant="tertiary"
          disabled={assigning || transitioning}
          onClick={() => finish()}
        >
          {t('onboarding.dismiss')}
        </Button>
      </header>

      <nav aria-label={t('onboarding.progress')}>
        <ol class="onboarding-progress">
          {#each coreStepIds as id, index (id)}
            <li class:current={step === id} class:complete={isStepComplete(id)}>
              <button
                type="button"
                aria-current={step === id ? 'step' : undefined}
                disabled={assigning ||
                  transitioning ||
                  (id === 'model' && !connectedProvider)}
                onclick={() =>
                  id === 'model' ? goToModelStep() : moveToStep(id)}
              >
                <span class="onboarding-step-number" aria-hidden="true"
                  >{index + 1}</span
                >
                <span>{stepLabel(id)}</span>
              </button>
            </li>
          {/each}
          {#if step === 'components' || step === 'services'}
            <li class="current" aria-current="step">
              <span>{t('onboarding.optional.badge')}</span>
            </li>
          {/if}
        </ol>
      </nav>

      <div class="onboarding-panel">
        <header class="onboarding-step-header">
          <h3
            class="onboarding-step-title"
            bind:this={stepHeading}
            tabindex="-1"
          >
            {stepTitle()}
          </h3>
          <p class="onboarding-step-subtitle">
            {stepDescription()}
          </p>
        </header>

        {#if setupError}<Banner variant="error" role="alert"
            >{setupError}</Banner
          >{/if}

        {#if settingsError}
          <Banner variant="error" role="alert">
            {settingsError}
            <Button
              variant="secondary"
              disabled={loadingSettings}
              onClick={step === 'provider' ? loadSettings : retryModels}
              >{t('onboarding.retry')}</Button
            >
          </Banner>
        {/if}

        {#if step === 'provider'}
          {#if loadingSettings && !settings}
            <p class="onboarding-loading" role="status">
              <span class="s-inline-spinner" aria-hidden="true"></span>{t(
                'onboarding.service.loading',
              )}
            </p>
          {:else if settings}
            <OnboardingProviders
              {settings}
              disabled={loadingSettings ||
                transitioning ||
                Boolean(settingsError)}
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
                  >{t('onboarding.service.connectedCount', {
                    count: usableProviders.length,
                  })}</strong
                ><span>{t('onboarding.service.addMore')}</span>
              </p>
              <ul aria-label={t('onboarding.service.existing')}>
                {#each usableProviders as provider (provider.id)}
                  <li>{providerName(provider)}</li>
                {/each}
              </ul>
            {:else}
              <p>
                {t('onboarding.service.required')}
              </p>
            {/if}
          </div>
          <footer class="onboarding-footer">
            <p class="onboarding-footnote">
              {t('onboarding.service.later')}
            </p>
            <Button
              variant="primary"
              class="onboarding-continue"
              disabled={!connectedProvider ||
                loadingSettings ||
                Boolean(settingsError)}
              onClick={goToModelStep}
            >
              {t('onboarding.service.continue')}{@render arrow()}
            </Button>
          </footer>
        {:else if step === 'model'}
          <div class="onboarding-model-provider">
            {#if usableProviders.length > 1}
              <label for="onboarding-model-provider"
                >{t('onboarding.provider')}</label
              >
              <SearchableDropdown
                id="onboarding-model-provider"
                panelClass="onboarding-model-menu"
                value={connectedProvider}
                options={usableProviders.map((provider) => ({
                  value: provider.id,
                  label: providerName(provider),
                }))}
                disabled={assigning || transitioning}
                ariaLabel={t('onboarding.provider')}
                searchPlaceholder={t('onboarding.service.search')}
                onValueChange={selectModelProvider}
              />
            {:else if activeProvider}
              <div class="onboarding-connection-summary">
                <span class="onboarding-connected-icon">{@render check()}</span
                ><strong>{providerName(activeProvider)}</strong><span
                  >{t('onboarding.service.connected')}</span
                >
              </div>
            {:else}
              <p class="onboarding-help">
                {t('onboarding.model.disconnected')}
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
                )}
              </p>
            {:else if modelsError}
              <Banner variant="error" role="alert">{modelsError}</Banner>
              <Button variant="secondary" onClick={retryModels}
                >{t('onboarding.retry')}</Button
              >
            {:else if !allModelOptions.length}
              <EmptyState
                density="compact"
                title={t('onboarding.model.emptyTitle')}
                description={t('onboarding.model.empty')}
              >
                {#snippet actions()}<Button
                    variant="secondary"
                    onClick={retryModels}>{t('onboarding.retry')}</Button
                  >{/snippet}
              </EmptyState>
            {:else}
              <div class="onboarding-model-label">
                <label for="onboarding-model"
                  >{t('onboarding.model.label')}</label
                >{#if connectedProvider === 'openrouter'}<Button
                    variant="secondary"
                    class="onboarding-free-filter"
                    aria-pressed={onlyFree}
                    disabled={assigning || transitioning}
                    onClick={() => {
                      onlyFree = !onlyFree;
                      if (
                        onlyFree &&
                        !parseModelSelectionValue(
                          selectedModelValue,
                        ).model.endsWith(':free')
                      ) {
                        selectedModelValue = '';
                        userClearedModel = true;
                      } else if (!onlyFree && !selectedModelValue) {
                        userClearedModel = false;
                        selectedModelValue = pickDefaultOnboardingModel(
                          suitableOptions,
                          { currentModel: savedAgentModel },
                        );
                      }
                    }}>{t('onboarding.model.freeOnly')}</Button
                  >{/if}
              </div>
              <SearchableDropdown
                id="onboarding-model"
                panelClass="onboarding-model-menu"
                value={selectedModelValue}
                options={modelOptions}
                disabled={assigning || transitioning}
                placeholder={t('onboarding.model.placeholder')}
                searchPlaceholder={t('onboarding.model.searchPlaceholder')}
                emptyLabel={onlyFree
                  ? t('onboarding.model.noFree')
                  : t('onboarding.model.searchEmpty')}
                ariaLabel={t('onboarding.model.label')}
                ariaDescribedby="onboarding-model-help"
                footerActionLabel={modelFilterFooter}
                onFooterAction={() => (showAllModels = !showAllModels)}
                onValueChange={(value) => {
                  selectedModelValue = value;
                  userClearedModel = !value;
                  assignError = '';
                }}
              />
              <p id="onboarding-model-help" class="onboarding-help">
                {t('onboarding.model.help')}
              </p>
              {#if selectedOption}
                <div class="onboarding-selection" role="status">
                  <span class="onboarding-selection-icon"
                    >{@render check()}</span
                  >
                  <div>
                    <strong>{selectedOption.label}</strong>
                    <p>
                      {t('onboarding.model.ready')}
                    </p>
                  </div>
                </div>
              {:else}
                <p class="onboarding-model-prompt">
                  {t('onboarding.model.prompt')}
                </p>
              {/if}
            {/if}
            {#if assignError}
              <Banner variant="error" role="alert">
                {assignError}
                <p>{t('onboarding.model.finishSavedHelp')}</p>
                <Button
                  variant="secondary"
                  disabled={assigning || transitioning}
                  onClick={() => finish(onDismiss, { saveSelection: false })}
                  >{t('onboarding.model.finishSaved')}</Button
                >
              </Banner>
            {/if}
          </div>

          <footer class="onboarding-footer">
            <Button
              variant="tertiary"
              disabled={assigning || transitioning}
              onClick={goToServiceStep}>{t('onboarding.model.back')}</Button
            >
            <div class="onboarding-footer-actions">
              <Button
                variant="secondary"
                disabled={assigning || transitioning}
                onClick={() => moveToStep('components')}
                >{t('onboarding.model.customize')}</Button
              >
              <Button
                variant="primary"
                disabled={!canStart}
                loading={assigning}
                onClick={() => finish(onComplete)}
                >{assigning
                  ? t('common.saving')
                  : t('onboarding.model.start')}{@render arrow()}</Button
              >
            </div>
          </footer>
        {:else}
          {#if step === 'components'}<OnboardingComponents
            />{:else}<OnboardingServices />{/if}
          <footer class="onboarding-footer">
            <Button
              variant="tertiary"
              disabled={transitioning}
              onClick={() =>
                moveToStep(step === 'services' ? 'components' : 'model')}
              >{step === 'services'
                ? t('onboarding.optional.backToComponents')
                : t('onboarding.optional.backToModel')}</Button
            >
            <div class="onboarding-footer-actions">
              {#if step === 'components'}<Button
                  variant="secondary"
                  disabled={transitioning}
                  onClick={() => moveToStep('services')}
                  >{t('onboarding.components.continue')}</Button
                >{/if}
              <Button
                variant="primary"
                disabled={transitioning}
                onClick={() => finish(onComplete)}
                >{t('onboarding.finish')}{@render arrow()}</Button
              >
            </div>
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
