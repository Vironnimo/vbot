<script>
  import { untrack } from 'svelte';

  import {
    listModels,
    listProviderRoutingOptions,
    updateSettings,
  } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';

  const PROVIDER_SLUG_PATTERN =
    /^[a-z0-9][a-z0-9._-]*(?:\/[a-z0-9][a-z0-9._-]*)*$/;
  const MODE_OPTIONS = [
    {
      value: 'automatic',
      labelKey: 'settings.providers.openrouter.mode.automatic',
      fallback: 'Automatic (cache-friendly)',
    },
    {
      value: 'allowed',
      labelKey: 'settings.providers.openrouter.mode.allowed',
      fallback: 'Only allowed providers',
    },
    {
      value: 'ordered',
      labelKey: 'settings.providers.openrouter.mode.ordered',
      fallback: 'Preferred provider order',
    },
  ];
  const noop = () => {};

  let {
    provider,
    active = false,
    onReloadSettings = noop,
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
      label: t(option.labelKey, option.fallback),
    })),
  );
  let saveError = $derived(validateRouting(routing));

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
        title: t(
          'settings.providers.openrouter.invalidSlug',
          'Enter a valid OpenRouter provider slug.',
        ),
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
      return t(
        'settings.providers.openrouter.providerRequired',
        '{scope} needs at least one provider for this routing mode.',
        { scope: label },
      );
    }
    for (const slug of policy.providers) {
      const blocked = [...policy.blocked, ...globallyBlocked].some(
        (blockedSlug) =>
          slug === blockedSlug || slug.startsWith(`${blockedSlug}/`),
      );
      if (blocked) {
        return t(
          'settings.providers.openrouter.providerConflict',
          '{provider} is both selected and blocked in {scope}.',
          { provider: slug, scope: label },
        );
      }
    }
    return '';
  }

  function validateRouting(value) {
    const defaultError = validatePolicy(
      value.default,
      t('settings.providers.openrouter.globalScope', 'Global routing'),
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

  async function saveRouting() {
    if (!dirty || saveError) {
      return;
    }
    saving = true;
    try {
      await updateSettings({
        providers: { openrouter: { routing } },
      });
      dirty = false;
      onError('');
      await onReloadSettings();
      onToast({
        title: t(
          'settings.providers.openrouter.saved',
          'OpenRouter routing settings saved.',
        ),
        variant: 'success',
      });
    } catch (error) {
      onToast({
        title:
          error?.message ||
          t(
            'settings.providers.openrouter.saveError',
            'OpenRouter routing settings could not be saved.',
          ),
        variant: 'error',
      });
    } finally {
      saving = false;
    }
  }
</script>

<div class="openrouter-routing">
  <div class="s-row-info">
    <div class="s-provider-connection-label">
      {t('settings.providers.openrouter.title', 'Routing')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.providers.openrouter.description',
        'Control which upstream providers OpenRouter may use. vBot also pins each Session to one endpoint to protect prompt-cache hits.',
      )}
    </div>
  </div>

  <FormField
    controlId="openrouter-routing-model"
    label={t('settings.providers.openrouter.scopeLabel', 'Scope')}
    help={t(
      'settings.providers.openrouter.scopeHelp',
      'Global routing applies to every OpenRouter model unless that model has an override.',
    )}
    full
  >
    {#snippet children({ controlId, describedBy })}
      <SearchableDropdown
        id={controlId}
        value={selectedModelId}
        options={[
          {
            value: '',
            label: t(
              'settings.providers.openrouter.globalScope',
              'Global routing',
            ),
          },
          ...modelOptions,
        ]}
        placeholder={t(
          'settings.providers.openrouter.globalScope',
          'Global routing',
        )}
        searchPlaceholder={t(
          'settings.providers.openrouter.modelSearch',
          'Find an OpenRouter model…',
        )}
        disabled={loadingModels}
        ariaDescribedby={describedBy}
        onValueChange={(value) => {
          selectedModelId = value;
          customProviderSlug = '';
        }}
      />
    {/snippet}
  </FormField>

  {#if selectedModelId}
    <div class="openrouter-routing__override-row">
      <div class="s-row-info">
        <div class="s-provider-connection-label">
          {t('settings.providers.openrouter.modelOverride', 'Model override')}
        </div>
        <div class="s-row-desc">
          {selectedHasOverride
            ? t(
                'settings.providers.openrouter.modelOverrideOn',
                'This model has its own routing policy. Global blocks still apply.',
              )
            : t(
                'settings.providers.openrouter.modelOverrideOff',
                'This model inherits the global routing policy.',
              )}
        </div>
      </div>
      <Toggle
        checked={selectedHasOverride}
        onChange={setModelOverride}
        ariaLabel={t(
          'settings.providers.openrouter.modelOverrideAria',
          'Use a routing override for {model}',
          { model: selectedModelId },
        )}
      />
    </div>
  {/if}

  <div class:openrouter-routing__disabled={!selectedHasOverride}>
    <FormField
      controlId="openrouter-routing-mode"
      label={t('settings.providers.openrouter.modeLabel', 'Routing mode')}
      full
    >
      {#snippet children({ controlId })}
        <Dropdown
          id={controlId}
          value={currentPolicy.mode}
          options={modeOptions}
          disabled={!selectedHasOverride}
          onValueChange={setMode}
        />
      {/snippet}
    </FormField>

    {#if currentPolicy.mode === 'ordered'}
      <Banner variant="warn">
        {t(
          'settings.providers.openrouter.orderWarning',
          'A manual provider order overrides OpenRouter Sticky Routing. OpenRouter tries the listed providers first, but automatic cache affinity is disabled.',
        )}
      </Banner>
    {/if}

    {#if currentPolicy.mode !== 'automatic'}
      <div class="openrouter-routing__list-block">
        <div class="s-provider-connection-label">
          {currentPolicy.mode === 'ordered'
            ? t(
                'settings.providers.openrouter.preferredProviders',
                'Provider priority',
              )
            : t(
                'settings.providers.openrouter.allowedProviders',
                'Allowed providers',
              )}
        </div>
        <div class="openrouter-routing__add-row">
          <SearchableDropdown
            value=""
            options={addableProviderOptions}
            placeholder={t(
              'settings.providers.openrouter.addProvider',
              'Add provider…',
            )}
            searchPlaceholder={t(
              'settings.providers.openrouter.providerSearch',
              'Find a provider…',
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
                        'Move {provider} up',
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
                        'Move {provider} down',
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
                      'Remove {provider}',
                      { provider: slug },
                    )}
                    onClick={() => removeProvider(slug, 'providers')}>×</Button
                  >
                </div>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <div class="openrouter-routing__list-block">
      <div class="s-provider-connection-label">
        {selectedModelId
          ? t(
              'settings.providers.openrouter.blockedProvidersModel',
              'Additionally blocked for this model',
            )
          : t(
              'settings.providers.openrouter.blockedProviders',
              'Blocked providers',
            )}
      </div>
      <div class="openrouter-routing__add-row">
        <SearchableDropdown
          value=""
          options={addableProviderOptions}
          placeholder={t(
            'settings.providers.openrouter.blockProvider',
            'Block provider…',
          )}
          searchPlaceholder={t(
            'settings.providers.openrouter.providerSearch',
            'Find a provider…',
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
                  'Unblock {provider}',
                  { provider: slug },
                )}
                onClick={() => removeProvider(slug, 'blocked')}>×</Button
              >
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <FormField
      controlId="openrouter-custom-provider"
      label={t(
        'settings.providers.openrouter.customProvider',
        'Custom provider slug',
      )}
      help={t(
        'settings.providers.openrouter.customProviderHelp',
        'Use an exact endpoint tag such as google-vertex/europe when it is not in the fetched list.',
      )}
      full
    >
      {#snippet children({ controlId, describedBy })}
        <div class="openrouter-routing__custom-row">
          <TextField
            id={controlId}
            value={customProviderSlug}
            placeholder={t(
              'settings.providers.openrouter.customProviderPlaceholder',
              'google-vertex/europe',
            )}
            aria-describedby={describedBy}
            disabled={!selectedHasOverride}
            onInput={(value) => (customProviderSlug = value)}
          />
          <Button
            variant="secondary"
            disabled={!selectedHasOverride || customProviderSlug.trim() === ''}
            onClick={() => addProvider(customProviderSlug, 'blocked')}
          >
            {t('settings.providers.openrouter.block', 'Block')}
          </Button>
          {#if currentPolicy.mode !== 'automatic'}
            <Button
              variant="secondary"
              disabled={!selectedHasOverride ||
                customProviderSlug.trim() === ''}
              onClick={() => addProvider(customProviderSlug, 'providers')}
            >
              {t('settings.providers.openrouter.select', 'Select')}
            </Button>
          {/if}
        </div>
      {/snippet}
    </FormField>

    <div class="openrouter-routing__fallback-row">
      <div class="s-row-info">
        <div class="s-provider-connection-label">
          {t('settings.providers.openrouter.fallbacks', 'Provider fallbacks')}
        </div>
        <div class="s-row-desc">
          {t(
            'settings.providers.openrouter.fallbacksHelp',
            'When disabled, OpenRouter returns an error instead of trying a backup provider when the primary is unavailable.',
          )}
        </div>
      </div>
      <Toggle
        checked={currentPolicy.allow_fallbacks}
        disabled={!selectedHasOverride}
        onChange={(value) => updateCurrentPolicy({ allow_fallbacks: value })}
        ariaLabel={t(
          'settings.providers.openrouter.fallbacksAria',
          'Allow OpenRouter provider fallbacks',
        )}
      />
    </div>
  </div>

  {#if saveError}
    <Banner variant="error">{saveError}</Banner>
  {/if}

  <div class="openrouter-routing__save-row">
    <Button
      variant="primary"
      disabled={!dirty || Boolean(saveError)}
      loading={saving}
      onClick={saveRouting}
    >
      {saving
        ? t('common.saving', 'Saving…')
        : t('settings.providers.openrouter.save', 'Save routing')}
    </Button>
  </div>
</div>
