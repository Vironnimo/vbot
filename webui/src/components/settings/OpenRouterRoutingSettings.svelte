<script>
  import { onDestroy, untrack } from 'svelte';

  import {
    listModels,
    listProviderRoutingOptions,
    updateSettings,
  } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import ProviderDetailDisclosure from './providers/ProviderDetailDisclosure.svelte';

  const PROVIDER_SLUG_PATTERN =
    /^[a-z0-9][a-z0-9._-]*(?:\/[a-z0-9][a-z0-9._-]*)*$/;
  const MODE_OPTIONS = [
    {
      value: 'automatic',
      label: () => t('settings.providers.openrouter.mode.automatic'),
    },
    {
      value: 'allowed',
      label: () => t('settings.providers.openrouter.mode.allowed'),
    },
    {
      value: 'ordered',
      label: () => t('settings.providers.openrouter.mode.ordered'),
    },
  ];
  const noop = () => {};

  let {
    provider,
    active = false,
    onRefreshProviderSettings = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  let routing = $state(untrack(() => normalizeRouting(provider?.routing)));
  let dirty = $state(false);
  let saving = $state(false);
  let loadingModels = $state(false);
  let loadingProviders = $state(false);
  let selectedModelId = $state('');
  let models = $state([]);
  let providerOptionsByScope = $state({});
  let customProviderSlug = $state('');
  let lastProviderRouting = $state('');

  let modelOptions = $derived(buildModelOptions(models, routing.models));
  let selectedHasOverride = $derived(
    selectedModelId === '' || Boolean(routing.models[selectedModelId]),
  );
  let currentPolicy = $derived(
    selectedModelId === ''
      ? routing.default
      : (routing.models[selectedModelId] ?? routing.default),
  );
  let currentProviderOptions = $derived(
    providerOptionsByScope[selectedModelId] ?? [],
  );
  let addableProviderOptions = $derived(
    currentProviderOptions
      .filter(
        (option) =>
          !currentPolicy.providers.includes(option.slug) &&
          !currentPolicy.blocked.includes(option.slug),
      )
      .map((option) => ({
        value: option.slug,
        label: option.name,
        secondaryLabel: option.slug,
        searchText: `${option.name} ${option.slug}`,
      })),
  );
  let modeOptions = $derived(
    MODE_OPTIONS.map((option) => ({
      value: option.value,
      label: option.label(),
    })),
  );
  let routingSummary = $derived(
    routing.default.mode === 'allowed'
      ? t('settings.providers.openrouter.summary.allowed')
      : routing.default.mode === 'ordered'
        ? t('settings.providers.openrouter.summary.ordered')
        : t('settings.providers.openrouter.summary.automatic'),
  );
  let saveError = $derived(validateRouting(routing));
  const autosaveContext = useAutosaveContext();
  const autosave = createDebouncedAutosave({
    getSnapshot: () => routing,
    hasChanges: () => dirty,
    save: saveRouting,
  });
  const unregisterAutosave = autosaveContext.register(autosave.participant);
  $effect(() => {
    if (!dirty || saving) return;
    autosave.scheduleRun();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    unregisterAutosave();
    autosave.cancelPendingTimer();
  });

  $effect(() => {
    const serialized = JSON.stringify(provider?.routing ?? {});
    if (!dirty && serialized !== lastProviderRouting) {
      routing = normalizeRouting(provider?.routing);
      lastProviderRouting = serialized;
    }
  });

  $effect(() => {
    if (active && models.length === 0 && !loadingModels) {
      void loadOpenRouterModels();
    }
    if (active && !providerOptionsByScope[''] && !loadingProviders) {
      void loadProviderOptions('');
    }
  });

  $effect(() => {
    if (
      active &&
      selectedModelId &&
      !providerOptionsByScope[selectedModelId] &&
      !loadingProviders
    ) {
      void loadProviderOptions(selectedModelId);
    }
  });

  function normalizePolicy(value) {
    const mode = ['automatic', 'allowed', 'ordered'].includes(value?.mode)
      ? value.mode
      : 'automatic';
    return {
      mode,
      providers: Array.isArray(value?.providers) ? [...value.providers] : [],
      blocked: Array.isArray(value?.blocked) ? [...value.blocked] : [],
      allow_fallbacks: value?.allow_fallbacks !== false,
    };
  }

  function buildModelOptions(catalog, overrides) {
    const options = catalog.map((model) => ({
      value: model.model_id,
      label: model.name || model.model_id,
      secondaryLabel: model.model_id,
      searchText: `${model.name ?? ''} ${model.model_id}`,
    }));
    const catalogIds = new Set(options.map((option) => option.value));
    for (const modelId of Object.keys(overrides)) {
      if (!catalogIds.has(modelId)) {
        options.push({
          value: modelId,
          label: modelId,
          secondaryLabel: '',
          searchText: modelId,
        });
      }
    }
    return options;
  }

  function normalizeRouting(value) {
    const normalized = {
      default: normalizePolicy(value?.default),
      models: {},
    };
    for (const [modelId, policy] of Object.entries(value?.models ?? {})) {
      normalized.models[modelId] = normalizePolicy(policy);
    }
    return normalized;
  }

  function updateCurrentPolicy(update) {
    if (!selectedHasOverride) {
      return;
    }
    const nextPolicy = { ...currentPolicy, ...update };
    if (selectedModelId === '') {
      routing = { ...routing, default: nextPolicy };
    } else {
      routing = {
        ...routing,
        models: { ...routing.models, [selectedModelId]: nextPolicy },
      };
    }
    dirty = true;
  }

  function setMode(mode) {
    updateCurrentPolicy({
      mode,
      providers: mode === 'automatic' ? [] : currentPolicy.providers,
    });
  }

  function setModelOverride(enabled) {
    if (!selectedModelId) {
      return;
    }
    if (enabled) {
      routing = {
        ...routing,
        models: {
          ...routing.models,
          [selectedModelId]: {
            ...routing.default,
            providers: [...routing.default.providers],
            blocked: [],
          },
        },
      };
    } else {
      const modelsWithoutOverride = { ...routing.models };
      delete modelsWithoutOverride[selectedModelId];
      routing = { ...routing, models: modelsWithoutOverride };
    }
    dirty = true;
  }

  function addProvider(slug, target) {
    const normalized = String(slug ?? '')
      .trim()
      .toLowerCase();
    if (!PROVIDER_SLUG_PATTERN.test(normalized)) {
      onToast({
        title: t('settings.providers.openrouter.invalidSlug'),
        variant: 'error',
      });
      return;
    }
    if (
      currentPolicy.providers.includes(normalized) ||
      currentPolicy.blocked.includes(normalized)
    ) {
      return;
    }
    updateCurrentPolicy({ [target]: [...currentPolicy[target], normalized] });
    customProviderSlug = '';
  }

  function removeProvider(slug, target) {
    updateCurrentPolicy({
      [target]: currentPolicy[target].filter((value) => value !== slug),
    });
  }

  function moveProvider(index, direction) {
    const nextIndex = index + direction;
    if (nextIndex < 0 || nextIndex >= currentPolicy.providers.length) {
      return;
    }
    const providers = [...currentPolicy.providers];
    [providers[index], providers[nextIndex]] = [
      providers[nextIndex],
      providers[index],
    ];
    updateCurrentPolicy({ providers });
  }

  async function loadOpenRouterModels() {
    loadingModels = true;
    try {
      const result = await listModels({
        provider_id: 'openrouter',
        task: 'chat',
      });
      models = Array.isArray(result?.models) ? result.models : [];
    } catch (error) {
      onError(error?.message || String(error));
    } finally {
      loadingModels = false;
    }
  }

  async function loadProviderOptions(modelId) {
    loadingProviders = true;
    try {
      const params = { provider_id: 'openrouter' };
      if (modelId) {
        params.model_id = modelId;
      }
      const result = await listProviderRoutingOptions(params);
      providerOptionsByScope = {
        ...providerOptionsByScope,
        [modelId]: Array.isArray(result?.providers) ? result.providers : [],
      };
    } catch (error) {
      onError(error?.message || String(error));
      providerOptionsByScope = { ...providerOptionsByScope, [modelId]: [] };
    } finally {
      loadingProviders = false;
    }
  }

  function validatePolicy(policy, label, globallyBlocked = []) {
    if (policy.mode !== 'automatic' && policy.providers.length === 0) {
      return t('settings.providers.openrouter.providerRequired', {
        scope: label,
      });
    }
    for (const slug of policy.providers) {
      const blocked = [...policy.blocked, ...globallyBlocked].some(
        (blockedSlug) =>
          slug === blockedSlug || slug.startsWith(`${blockedSlug}/`),
      );
      if (blocked) {
        return t('settings.providers.openrouter.providerConflict', {
          provider: slug,
          scope: label,
        });
      }
    }
    return '';
  }

  function validateRouting(value) {
    const defaultError = validatePolicy(
      value.default,
      t('settings.providers.openrouter.globalScope'),
    );
    if (defaultError) {
      return defaultError;
    }
    for (const [modelId, policy] of Object.entries(value.models)) {
      const modelError = validatePolicy(policy, modelId, value.default.blocked);
      if (modelError) {
        return modelError;
      }
    }
    return '';
  }

  async function saveRouting(reason) {
    if (!dirty) {
      if (reason === 'manual')
        onToast({
          title: t('common.alreadySaved'),
          variant: 'success',
        });
      return true;
    }
    if (saveError) return false;
    const submitted = JSON.stringify(routing);
    saving = true;
    try {
      await updateSettings({
        providers: { openrouter: { routing: JSON.parse(submitted) } },
      });
      onError('');
      await onRefreshProviderSettings();
      if (JSON.stringify(routing) === submitted) dirty = false;
      if (reason === 'manual')
        onToast({
          title: t('settings.providers.openrouter.saved'),
          variant: 'success',
        });
      return true;
    } catch (error) {
      onToast({
        title: error?.message || t('settings.providers.openrouter.saveError'),
        variant: 'error',
      });
      return false;
    } finally {
      saving = false;
    }
  }
</script>

<div class="openrouter-routing">
  <ProviderDetailDisclosure
    id="openrouter-routing"
    label={t('settings.providers.openrouter.title')}
    help={t('settings.providers.openrouter.help')}
    helpLabel={t('settings.providers.openrouter.helpAria')}
    summary={routingSummary}
  >
    <div class="openrouter-routing__rows">
      <div class="openrouter-routing__row">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('settings.providers.openrouter.scopeLabel')}
            <InfoHint text={t('settings.providers.openrouter.scopeHelp')} />
          </div>
        </div>
        <div class="openrouter-routing__control">
          <SearchableDropdown
            id="openrouter-routing-model"
            value={selectedModelId}
            options={[
              {
                value: '',
                label: t('settings.providers.openrouter.globalScope'),
              },
              ...modelOptions,
            ]}
            placeholder={t('settings.providers.openrouter.globalScope')}
            searchPlaceholder={t('settings.providers.openrouter.modelSearch')}
            disabled={loadingModels}
            onValueChange={(value) => {
              selectedModelId = value;
              customProviderSlug = '';
            }}
          />
        </div>
      </div>

      {#if selectedModelId}
        <div
          class="openrouter-routing__row openrouter-routing__row--compact openrouter-routing__override-row"
        >
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.providers.openrouter.modelOverride')}
            </div>
            <div class="s-row-desc">
              {selectedHasOverride
                ? t('settings.providers.openrouter.modelOverrideOn')
                : t('settings.providers.openrouter.modelOverrideOff')}
            </div>
          </div>
          <Toggle
            checked={selectedHasOverride}
            onChange={setModelOverride}
            ariaLabel={t('settings.providers.openrouter.modelOverrideAria', {
              model: selectedModelId,
            })}
          />
        </div>
      {/if}

      <div
        class="openrouter-routing__rows"
        class:openrouter-routing__disabled={!selectedHasOverride}
      >
        <div class="openrouter-routing__row">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.providers.openrouter.modeLabel')}
            </div>
          </div>
          <div class="openrouter-routing__control">
            <Dropdown
              id="openrouter-routing-mode"
              value={currentPolicy.mode}
              options={modeOptions}
              ariaLabel={t('settings.providers.openrouter.modeLabel')}
              disabled={!selectedHasOverride}
              onValueChange={setMode}
            />
          </div>
        </div>

        {#if currentPolicy.mode === 'ordered'}
          <Banner variant="warn">
            {t('settings.providers.openrouter.orderWarning')}
          </Banner>
        {/if}

        {#if currentPolicy.mode !== 'automatic'}
          <div class="openrouter-routing__row">
            <div class="s-row-info">
              <div class="s-row-label">
                {currentPolicy.mode === 'ordered'
                  ? t('settings.providers.openrouter.preferredProviders')
                  : t('settings.providers.openrouter.allowedProviders')}
              </div>
            </div>
            <div class="openrouter-routing__control">
              <div class="openrouter-routing__add-row">
                <SearchableDropdown
                  value=""
                  options={addableProviderOptions}
                  placeholder={t('settings.providers.openrouter.addProvider')}
                  searchPlaceholder={t(
                    'settings.providers.openrouter.providerSearch',
                  )}
                  disabled={!selectedHasOverride || loadingProviders}
                  onValueChange={(value) => addProvider(value, 'providers')}
                />
              </div>
              {#if currentPolicy.providers.length > 0}
                <div class="openrouter-routing__provider-list">
                  {#each currentPolicy.providers as slug, index (slug)}
                    <div class="openrouter-routing__provider-row">
                      <code>{slug}</code>
                      <div class="openrouter-routing__provider-actions">
                        {#if currentPolicy.mode === 'ordered'}
                          <Button
                            variant="tertiary"
                            icon
                            disabled={!selectedHasOverride || index === 0}
                            ariaLabel={t(
                              'settings.providers.openrouter.moveUp',
                              { provider: slug },
                            )}
                            onClick={() => moveProvider(index, -1)}>↑</Button
                          >
                          <Button
                            variant="tertiary"
                            icon
                            disabled={!selectedHasOverride ||
                              index === currentPolicy.providers.length - 1}
                            ariaLabel={t(
                              'settings.providers.openrouter.moveDown',
                              { provider: slug },
                            )}
                            onClick={() => moveProvider(index, 1)}>↓</Button
                          >
                        {/if}
                        <Button
                          variant="tertiary"
                          icon
                          disabled={!selectedHasOverride}
                          ariaLabel={t(
                            'settings.providers.openrouter.removeProvider',
                            { provider: slug },
                          )}
                          onClick={() => removeProvider(slug, 'providers')}
                          >×</Button
                        >
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          </div>
        {/if}

        <div class="openrouter-routing__row">
          <div class="s-row-info">
            <div class="s-row-label">
              {selectedModelId
                ? t('settings.providers.openrouter.blockedProvidersModel')
                : t('settings.providers.openrouter.blockedProviders')}
            </div>
          </div>
          <div class="openrouter-routing__control">
            <div class="openrouter-routing__add-row">
              <SearchableDropdown
                value=""
                options={addableProviderOptions}
                placeholder={t('settings.providers.openrouter.blockProvider')}
                searchPlaceholder={t(
                  'settings.providers.openrouter.providerSearch',
                )}
                disabled={!selectedHasOverride || loadingProviders}
                onValueChange={(value) => addProvider(value, 'blocked')}
              />
            </div>
            {#if currentPolicy.blocked.length > 0}
              <div class="openrouter-routing__provider-list">
                {#each currentPolicy.blocked as slug (slug)}
                  <div class="openrouter-routing__provider-row">
                    <code>{slug}</code>
                    <Button
                      variant="tertiary"
                      icon
                      disabled={!selectedHasOverride}
                      ariaLabel={t(
                        'settings.providers.openrouter.unblockProvider',
                        { provider: slug },
                      )}
                      onClick={() => removeProvider(slug, 'blocked')}>×</Button
                    >
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        </div>

        <div
          class="openrouter-routing__row openrouter-routing__row--compact openrouter-routing__fallback-row"
        >
          <div class="s-row-info">
            <div class="s-row-label">
              {t('settings.providers.openrouter.fallbacks')}
            </div>
            <div class="s-row-desc">
              {t('settings.providers.openrouter.fallbacksDescription')}
            </div>
          </div>
          <Toggle
            checked={currentPolicy.allow_fallbacks}
            disabled={!selectedHasOverride}
            onChange={(value) =>
              updateCurrentPolicy({ allow_fallbacks: value })}
            ariaLabel={t('settings.providers.openrouter.fallbacksAria')}
          />
        </div>

        <div class="openrouter-routing__row">
          <div class="s-row-info">
            <div class="s-row-label">
              <label for="openrouter-custom-provider">
                {t('settings.providers.openrouter.customProvider')}
              </label>
              <InfoHint
                text={t('settings.providers.openrouter.customProviderHelp')}
              />
            </div>
          </div>
          <div class="openrouter-routing__control">
            <div class="openrouter-routing__custom-row">
              <TextField
                id="openrouter-custom-provider"
                code
                value={customProviderSlug}
                placeholder={t(
                  'settings.providers.openrouter.customProviderPlaceholder',
                )}
                disabled={!selectedHasOverride}
                onInput={(value) => (customProviderSlug = value)}
              />
              <Button
                variant="secondary"
                disabled={!selectedHasOverride ||
                  customProviderSlug.trim() === ''}
                onClick={() => addProvider(customProviderSlug, 'blocked')}
              >
                {t('settings.providers.openrouter.block')}
              </Button>
              {#if currentPolicy.mode !== 'automatic'}
                <Button
                  variant="secondary"
                  disabled={!selectedHasOverride ||
                    customProviderSlug.trim() === ''}
                  onClick={() => addProvider(customProviderSlug, 'providers')}
                >
                  {t('settings.providers.openrouter.select')}
                </Button>
              {/if}
            </div>
          </div>
        </div>
      </div>

      <div class="openrouter-routing__save-row">
        <Button
          variant="tertiary"
          disabled={saving}
          loading={saving}
          onClick={() =>
            autosave.participant.runSave('manual', { force: true })}
        >
          {saving
            ? t('common.saving')
            : t('settings.providers.openrouter.save')}
        </Button>
      </div>
    </div>
  </ProviderDetailDisclosure>

  {#if saveError}
    <Banner variant="error">{saveError}</Banner>
  {/if}
</div>
