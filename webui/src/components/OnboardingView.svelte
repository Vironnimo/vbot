<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { englishCatalog, t } from '$lib/i18n.js';
  import {
    AGENT_FORM_MODE_EDIT,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { buildModelSelectOptions } from '$lib/modelSelection.js';
  import {
    ONBOARDING_TARGET_AGENT_ID,
    connectedProviderId,
    isOperational,
    onboardingHeroScope,
    onboardingMoreProviders,
    onboardingSubscriptionProviders,
    providerModalScope,
    providerModelSearchPrefill,
    providerTipKey,
  } from '$lib/onboarding.js';

  import ProviderConnectModal from './settings/ProviderConnectModal.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import Button from './ui/Button.svelte';
  import '../styles/onboarding.css';

  const noop = () => {};

  let {
    // The agent that receives the chosen model. A fresh install seeds `main`.
    targetAgentId = ONBOARDING_TARGET_AGENT_ID,
    // Forwarded to the connect modal so the OAuth device flow can complete.
    providerAuthEvent = null,
    // Bumped by App on a provider/model change; re-derives operational state so
    // an out-of-band connect (Settings panel) is reflected here too.
    modelsRefreshToken = 0,
    onComplete = noop,
    onDismiss = noop,
    onToast = noop,
  } = $props();

  const STEP_SERVICE = 'service';
  const STEP_MODEL = 'model';

  let settings = $state(null);
  let settingsError = $state('');
  let step = $state(STEP_SERVICE);
  let modalScope = $state(null);
  let moreOpen = $state(false);
  let lastModelsRefreshToken = null;

  let models = $state([]);
  let connections = $state([]);
  let loadingModels = $state(false);
  let modelsError = $state('');
  let selectedModelValue = $state('');
  let assigning = $state(false);
  let assignError = $state('');

  let operational = $derived(isOperational(settings));
  let heroScope = $derived(onboardingHeroScope(settings));
  let subscriptionProviders = $derived(
    onboardingSubscriptionProviders(settings),
  );
  let moreProviders = $derived(onboardingMoreProviders(settings));
  let connectedProvider = $derived(connectedProviderId(settings));
  let searchPrefill = $derived(providerModelSearchPrefill(connectedProvider));
  let hasModels = $derived(models.length > 0);
  let tipText = $derived(providerTip(connectedProvider));
  let modelOptions = $derived(
    buildModelSelectOptions({
      models,
      connections,
      selectedModelValue,
      emptyLabel: t('onboarding.model.placeholder', 'Select a model'),
      translate: t,
    }),
  );

  onMount(() => {
    void loadSettings();
  });

  // React to a provider/model change: reload settings + models, and advance to
  // the model step if a service got connected while the service step was open.
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void reflectResourceChange();
    }
  });

  async function reflectResourceChange() {
    await loadSettings();
    if (operational && step === STEP_SERVICE) {
      goToModelStep();
    } else if (step === STEP_MODEL) {
      void loadModels();
    }
  }

  async function loadSettings() {
    try {
      settings = await rpc('settings.get');
      settingsError = '';
    } catch (error) {
      settingsError = `${t('onboarding.model.loadError', 'Models could not be loaded.')} ${error.message}`;
    }
  }

  function providerName(provider) {
    return provider?.name ?? provider?.id ?? 'Provider';
  }

  // Render a provider's model-step tip only when the catalog actually carries
  // the key (most providers have none), so an unknown provider gets nothing.
  function providerTip(providerId) {
    const key = providerTipKey(providerId);
    return key && englishCatalog[key] ? t(key) : '';
  }

  function openScope(scope) {
    if (scope) {
      modalScope = scope;
    }
  }

  // The connect modal drives credential entry; on success it advances to the
  // model step. The shipped per-provider catalog already fills the dropdown, so
  // model.list runs immediately while the DB refresh happens in the background.
  function handleConnected() {
    step = STEP_MODEL;
    void loadSettings();
    void loadModels();
    void refreshModelDatabase();
  }

  function goToModelStep() {
    step = STEP_MODEL;
    void loadModels();
  }

  async function loadModels() {
    const hadModels = models.length > 0;
    if (!hadModels) {
      loadingModels = true;
    }
    modelsError = '';
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        rpc('model.list'),
        rpc('connection.list'),
      ]);
      models = Array.isArray(modelsResult?.models) ? modelsResult.models : [];
      connections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
    } catch (error) {
      if (!hadModels) {
        modelsError = `${t('onboarding.model.loadError', 'Models could not be loaded.')} ${error.message}`;
      }
    } finally {
      loadingModels = false;
    }
  }

  // Best-effort: the shipped catalog already populated the dropdown, so a failed
  // refresh must never block the model step. On success the freshened catalog
  // reloads silently (the loading state stays hidden once models are present).
  async function refreshModelDatabase() {
    try {
      await rpc('model.refresh_db');
    } catch {
      // Ignored — the shipped catalog is enough to pick a model.
    }
    await loadModels();
  }

  function retryModels() {
    void loadSettings();
    void refreshModelDatabase();
  }

  async function startChatting() {
    if (!selectedModelValue || assigning) {
      return;
    }
    assigning = true;
    assignError = '';
    try {
      const current = await rpc('agent.get', { id: targetAgentId });
      const baseline = createAgentFormValues(current);
      const result = normalizeAgentForm(
        { ...baseline, model: selectedModelValue },
        { mode: AGENT_FORM_MODE_EDIT, initialValues: baseline },
      );
      if (!result.isValid) {
        assignError = t(
          'onboarding.model.assignError',
          'The model could not be assigned.',
        );
        return;
      }
      await rpc('agent.update', result.payload);
      onComplete();
    } catch (error) {
      assignError = `${t('onboarding.model.assignError', 'The model could not be assigned.')} ${error.message}`;
    } finally {
      assigning = false;
    }
  }
</script>

<section class="view onboarding-view active" aria-labelledby="onboarding-title">
  <div class="onboarding-scroll">
    <div class="onboarding-column">
      <header class="onboarding-header">
        <h2 id="onboarding-title" class="onboarding-title">
          {t('onboarding.title', 'Set up vBot')}
        </h2>
        <Button
          variant="tertiary"
          class="onboarding-dismiss"
          onClick={onDismiss}
        >
          {t('onboarding.dismiss', 'Skip for now')}
        </Button>
      </header>

      {#if step === STEP_SERVICE}
        <div class="onboarding-step">
          <p class="onboarding-kicker">
            {t('onboarding.step.service.kicker', 'Step 1 of 3')}
          </p>
          <h3 class="onboarding-step-title">
            {t('onboarding.step.service.title', 'Choose an AI service')}
          </h3>
          <p class="onboarding-step-subtitle">
            {t(
              'onboarding.step.service.subtitle',
              'vBot reaches AI models through a service. Pick one to connect — you can add more later in Settings.',
            )}
          </p>

          {#if settingsError}
            <p class="onboarding-error" role="alert">{settingsError}</p>
          {/if}

          {#if heroScope}
            <button
              type="button"
              class="onboarding-hero"
              onclick={() => openScope(heroScope)}
            >
              <span class="onboarding-hero-badge">
                {t('onboarding.hero.badge', 'Recommended to start')}
              </span>
              <span class="onboarding-hero-title">
                {t('onboarding.hero.title', 'OpenRouter')}
              </span>
              <span class="onboarding-hero-desc">
                {t(
                  'onboarding.hero.description',
                  'One account unlocks many models, including free ones.',
                )}
              </span>
              <span class="onboarding-hero-action">
                {t('onboarding.hero.action', 'Connect OpenRouter')}
              </span>
            </button>
          {/if}

          {#if subscriptionProviders.length > 0}
            <div class="onboarding-card">
              <div class="onboarding-card-copy">
                <h4 class="onboarding-card-title">
                  {t('onboarding.subscription.title', 'Already subscribed?')}
                </h4>
                <p class="onboarding-card-desc">
                  {t(
                    'onboarding.subscription.description',
                    'Sign in with an existing subscription — no API key needed.',
                  )}
                </p>
              </div>
              <div class="onboarding-card-actions">
                {#each subscriptionProviders as provider (provider.id)}
                  <Button
                    variant="secondary"
                    onClick={() =>
                      openScope(providerModalScope(provider, 'oauth'))}
                  >
                    {t(
                      'onboarding.subscription.action',
                      'Sign in with {provider}',
                      {
                        provider: providerName(provider),
                      },
                    )}
                  </Button>
                {/each}
              </div>
            </div>
          {/if}

          {#if moreProviders.length > 0}
            <div class="onboarding-more">
              <button
                type="button"
                class="onboarding-more-toggle"
                aria-expanded={moreOpen}
                onclick={() => (moreOpen = !moreOpen)}
              >
                <span>{t('onboarding.more.toggle', 'More services')}</span>
                <svg
                  class="onboarding-more-chevron"
                  class:onboarding-more-chevron--open={moreOpen}
                  viewBox="0 0 12 12"
                  width="10"
                  height="10"
                  aria-hidden="true"
                >
                  <path d="M2 4l4 4 4-4" />
                </svg>
              </button>
              {#if moreOpen}
                <div class="onboarding-more-panel">
                  <p class="onboarding-card-desc">
                    {t(
                      'onboarding.more.description',
                      'Connect another provider with an API key.',
                    )}
                  </p>
                  <div class="onboarding-more-list">
                    {#each moreProviders as provider (provider.id)}
                      <Button
                        variant="secondary"
                        onClick={() =>
                          openScope(providerModalScope(provider, 'api_key'))}
                      >
                        {t('onboarding.more.action', 'Connect {provider}', {
                          provider: providerName(provider),
                        })}
                      </Button>
                    {/each}
                  </div>
                </div>
              {/if}
            </div>
          {/if}

          {#if operational}
            <div class="onboarding-footer">
              <Button variant="primary" onClick={goToModelStep}>
                {t('onboarding.step.model.title', 'Choose a model')}
              </Button>
            </div>
          {/if}
        </div>
      {:else}
        <div class="onboarding-step">
          <p class="onboarding-kicker">
            {t('onboarding.step.model.kicker', 'Step 3 of 3')}
          </p>
          <h3 class="onboarding-step-title">
            {t('onboarding.step.model.title', 'Choose a model')}
          </h3>
          <p class="onboarding-step-subtitle">
            {t(
              'onboarding.step.model.subtitle',
              'Pick the model this agent will use. You can change it anytime in Agents.',
            )}
          </p>

          {#if loadingModels && !hasModels}
            <p class="onboarding-notice" aria-live="polite">
              {t('onboarding.model.loading', 'Loading models…')}
            </p>
          {:else if modelsError}
            <p class="onboarding-error" role="alert">{modelsError}</p>
            <div class="onboarding-footer">
              <Button variant="secondary" onClick={retryModels}>
                {t('onboarding.model.retry', 'Retry')}
              </Button>
            </div>
          {:else if !hasModels}
            <p class="onboarding-notice">
              {t(
                'onboarding.model.empty',
                'No models are available yet. Retry once the model list finishes updating.',
              )}
            </p>
            <div class="onboarding-footer">
              <Button variant="secondary" onClick={retryModels}>
                {t('onboarding.model.retry', 'Retry')}
              </Button>
            </div>
          {:else}
            {#if tipText}
              <p class="onboarding-tip">{tipText}</p>
            {/if}
            <label class="onboarding-field">
              <span class="onboarding-field-label">
                {t('onboarding.model.label', 'Model')}
              </span>
              {#key searchPrefill}
                <SearchableDropdown
                  id="onboarding-model"
                  value={selectedModelValue}
                  options={modelOptions}
                  searchText={searchPrefill}
                  placeholder={t(
                    'onboarding.model.placeholder',
                    'Select a model',
                  )}
                  searchPlaceholder={t(
                    'onboarding.model.searchPlaceholder',
                    'Filter models…',
                  )}
                  emptyLabel={t(
                    'onboarding.model.searchEmpty',
                    'No models match',
                  )}
                  ariaLabel={t('onboarding.model.label', 'Model')}
                  onValueChange={(value) => (selectedModelValue = value)}
                />
              {/key}
            </label>

            {#if assignError}
              <p class="onboarding-error" role="alert">{assignError}</p>
            {/if}

            <div class="onboarding-footer">
              <Button variant="secondary" onClick={() => (step = STEP_SERVICE)}>
                {t('onboarding.model.back', 'Choose a different service')}
              </Button>
              <Button
                variant="primary"
                disabled={!selectedModelValue || assigning}
                onClick={startChatting}
              >
                {assigning
                  ? t('common.saving', 'Saving…')
                  : t('onboarding.model.start', 'Start chatting')}
              </Button>
            </div>
          {/if}
        </div>
      {/if}
    </div>
  </div>

  {#if modalScope}
    <ProviderConnectModal
      providers={modalScope.providers}
      scopedProvider={modalScope.scopedProvider}
      scopedConnection={modalScope.scopedConnection}
      {providerAuthEvent}
      {onToast}
      onCompleted={handleConnected}
      onClose={() => (modalScope = null)}
    />
  {/if}
</section>
