<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import { listConnections, listModels } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildModelSelectOptions,
    createModelCatalogLoader,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import {
    AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
    buildAgentDefaultsPayload,
    normalizeAgentDefaultsSettings,
  } from '$lib/settingsView.js';
  import {
    SURFACE_FORM,
    shouldApplyReloadNow,
  } from '$lib/resourceInvalidation.js';

  const noop = () => {};
  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const AGENT_THINKING_EFFORT_OPTIONS = Object.freeze([
    'none',
    'minimal',
    'low',
    'medium',
    'high',
    'xhigh',
    'max',
  ]);

  function normalizeAgentDefaultsFormValues(rawSettings) {
    const normalized = normalizeAgentDefaultsSettings(rawSettings);

    return {
      model: normalized.model,
      fallback_model: normalized.fallback_model,
      temperature:
        normalized.temperature === null ? '' : String(normalized.temperature),
      thinking_effort:
        normalized.thinking_effort === null
          ? AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT
          : normalized.thinking_effort,
    };
  }

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
    modelsRefreshToken = 0,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let agentDefaults = $state(
    untrack(() => normalizeAgentDefaultsFormValues(settings)),
  );
  let saving = $state(false);
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let autoSaveTimer = null;
  // A live model reload fetches in the background but holds the visible option
  // swap while a picker is open, so an open selection is never disturbed; the
  // form's selected value lives in `agentDefaults`, separate from these options.
  let modelDropdownOpenCount = $state(0);
  let pendingModelCatalogs = null;
  let lastModelsRefreshToken = null;

  let showAllModels = $state(false);
  let showAllFallbackModels = $state(false);
  let allDefaultModelOptions = $derived(
    selectModelOptions(
      agentDefaults.model,
      t('settings.defaults.noModelDefault', '— (no default)'),
    ),
  );
  let allDefaultFallbackModelOptions = $derived(
    selectModelOptions(
      agentDefaults.fallback_model,
      t('settings.defaults.noFallbackModelDefault', '— (no default)'),
    ),
  );
  let defaultModelOptions = $derived(
    filterModelSelectOptions(allDefaultModelOptions, {
      showAll: showAllModels,
      selectedModelValue: agentDefaults.model,
    }),
  );
  let defaultFallbackModelOptions = $derived(
    filterModelSelectOptions(allDefaultFallbackModelOptions, {
      showAll: showAllFallbackModels,
      selectedModelValue: agentDefaults.fallback_model,
    }),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allDefaultModelOptions.length - defaultModelOptions.length,
      translate: t,
    }),
  );
  let fallbackModelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllFallbackModels,
      hiddenCount:
        allDefaultFallbackModelOptions.length -
        defaultFallbackModelOptions.length,
      translate: t,
    }),
  );
  let defaultModelSelectValue = $derived(
    selectModelValue(agentDefaults.model, defaultModelOptions),
  );
  let defaultFallbackModelSelectValue = $derived(
    selectModelValue(agentDefaults.fallback_model, defaultFallbackModelOptions),
  );
  let thinkingEffortOptions = $derived([
    {
      value: AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
      label: t('settings.defaults.noThinkingEffort', '— (no default)'),
    },
    {
      value: '',
      label: t(
        'settings.defaults.providerThinkingEffortDefault',
        '— (provider default)',
      ),
    },
    ...AGENT_THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label: t(`agents.form.thinkingEffortOption.${option}`, option),
    })),
  ]);
  let saveDisabled = $derived(
    saving || agentDefaultsMatch(agentDefaults, settings),
  );
  const autosaveContext = useAutosaveContext();
  const modelCatalogLoader = createModelCatalogLoader({
    listModels,
    listConnections,
  });
  const agentDefaultsAutosave = createAutosaveParticipant({
    cancelPending: clearAutoSaveTimer,
    getSnapshot: () => ({ ...agentDefaults }),
    hasChanges: agentDefaultsDraftHasChanges,
    save: saveAgentDefaults,
  });
  const unregisterAgentDefaultsAutosave = autosaveContext.register(
    agentDefaultsAutosave,
  );

  onMount(() => {
    void loadModelCatalogs();
  });

  onDestroy(() => {
    modelCatalogLoader.invalidate();
    unregisterAgentDefaultsAutosave();
    clearAutoSaveTimer();
  });

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void agentDefaultsAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  // Reload the model catalog when the generic invalidation channel signals a
  // model/provider change (first run is a no-op: mount already loaded).
  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void reloadModelCatalogs();
    }
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  function applyModelCatalogs(catalogs) {
    availableModels = catalogs.models;
    availableConnections = catalogs.connections;
    pendingModelCatalogs = null;
  }

  async function loadModelCatalogs() {
    pendingModelCatalogs = null;
    try {
      const catalogs = await modelCatalogLoader.load();
      if (catalogs !== null) {
        applyModelCatalogs(catalogs);
      }
    } catch (error) {
      onError(
        `${t('settings.models.loadError', 'Model catalog could not be loaded.')} ${error.message}`,
      );
    }
  }

  async function reloadModelCatalogs() {
    pendingModelCatalogs = null;
    let catalogs;
    try {
      catalogs = await modelCatalogLoader.load();
    } catch (error) {
      onError(
        `${t('settings.models.loadError', 'Model catalog could not be loaded.')} ${error.message}`,
      );
      return;
    }
    if (catalogs === null) {
      return;
    }
    if (
      shouldApplyReloadNow(SURFACE_FORM, {
        dropdownOpen: modelDropdownOpenCount > 0,
      })
    ) {
      applyModelCatalogs(catalogs);
    } else {
      pendingModelCatalogs = catalogs;
    }
  }

  function trackModelDropdownOpen(open) {
    modelDropdownOpenCount = Math.max(
      0,
      modelDropdownOpenCount + (open ? 1 : -1),
    );
    if (modelDropdownOpenCount === 0 && pendingModelCatalogs) {
      applyModelCatalogs(pendingModelCatalogs);
    }
  }

  function selectModelOptions(selectedModelValue, emptyLabel) {
    return buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue,
      emptyLabel,
      translate: t,
    });
  }

  function agentDefaultsMatch(left, right) {
    const normalizedLeft = normalizeAgentDefaultsSettings(left);
    const normalizedRight = normalizeAgentDefaultsSettings(right);

    return (
      normalizedLeft.model === normalizedRight.model &&
      normalizedLeft.fallback_model === normalizedRight.fallback_model &&
      normalizedLeft.temperature === normalizedRight.temperature &&
      normalizedLeft.thinking_effort === normalizedRight.thinking_effort
    );
  }

  function agentDefaultsDraftHasChanges() {
    const persisted = normalizeAgentDefaultsFormValues(settings);
    return Object.keys(persisted).some(
      (key) => agentDefaults[key] !== persisted[key],
    );
  }

  function handleAgentDefaultsChange(key, value) {
    agentDefaults = {
      ...agentDefaults,
      [key]: value,
    };
    onError('');
  }

  function updateAgentDefaultsModelSelection(key, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    handleAgentDefaultsChange(
      key,
      modelSelectionValue(selection.model, selection.connectionLocalId),
    );
  }

  function handleManualAgentDefaultsSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }

    clearAutoSaveTimer();
    void agentDefaultsAutosave.runSave('manual');
  }

  async function saveAgentDefaults() {
    if (!agentDefaultsDraftHasChanges()) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildAgentDefaultsPayload(agentDefaults),
      successKey: 'settings.defaults.saveSuccess',
      successFallback: 'Agent defaults updated.',
      getDraftSnapshot: () => agentDefaults,
      applyResult: (next) =>
        (agentDefaults = normalizeAgentDefaultsFormValues(next)),
    });
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.model', 'Model')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.defaults.modelDescription',
        'Used when an agent model is empty.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--model">
    <SearchableDropdown
      id="settings-defaults-model"
      value={defaultModelSelectValue}
      options={defaultModelOptions}
      placeholder={t('settings.defaults.noModelDefault', '— (no default)')}
      searchPlaceholder={t(
        'agents.form.modelSearchPlaceholder',
        'Filter models…',
      )}
      emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
      ariaLabel={t('settings.defaults.model', 'Model')}
      triggerClass="settings-view__dropdown"
      panelClass="settings-view__model-panel"
      footerActionLabel={modelFilterFooter}
      onFooterAction={() => (showAllModels = !showAllModels)}
      onOpenChange={trackModelDropdownOpen}
      onValueChange={(selectedValue) =>
        updateAgentDefaultsModelSelection('model', selectedValue)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.fallbackModel', 'Fallback model')}
      <InfoHint
        text={t(
          'agents.form.fallbackModelHelp',
          'Used automatically when the primary model fails or is unavailable.',
        )}
      />
    </div>
    <div class="s-row-desc">
      {t(
        'settings.defaults.fallbackModelDescription',
        'Used when an agent fallback model is empty.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--model">
    <SearchableDropdown
      id="settings-defaults-fallback-model"
      value={defaultFallbackModelSelectValue}
      options={defaultFallbackModelOptions}
      placeholder={t(
        'settings.defaults.noFallbackModelDefault',
        '— (no default)',
      )}
      searchPlaceholder={t(
        'agents.form.modelSearchPlaceholder',
        'Filter models…',
      )}
      emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
      ariaLabel={t('settings.defaults.fallbackModel', 'Fallback model')}
      triggerClass="settings-view__dropdown"
      panelClass="settings-view__model-panel"
      footerActionLabel={fallbackModelFilterFooter}
      onFooterAction={() => (showAllFallbackModels = !showAllFallbackModels)}
      onOpenChange={trackModelDropdownOpen}
      onValueChange={(selectedValue) =>
        updateAgentDefaultsModelSelection('fallback_model', selectedValue)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.temperature', 'Temperature')}
      <InfoHint
        text={t(
          'agents.form.temperatureHelp',
          'Sampling randomness, typically 0–2. Leave empty to use the default.',
        )}
      />
    </div>
    <div class="s-row-desc">
      {t(
        'settings.defaults.temperatureDescription',
        'Used when an agent temperature is unset.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <TextField
      id="settings-defaults-temperature"
      inputmode="decimal"
      value={agentDefaults.temperature}
      ariaLabel={t('settings.defaults.temperature', 'Temperature')}
      onInput={(next) => handleAgentDefaultsChange('temperature', next)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.thinkingEffort', 'Thinking effort')}
      <InfoHint
        text={t(
          'agents.form.thinkingEffortHelp',
          'How much internal reasoning the model may spend before answering. Leave at — for the default.',
        )}
      />
    </div>
    <div class="s-row-desc">
      {t(
        'settings.defaults.thinkingEffortDescription',
        'Used when an agent thinking effort is unset.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--model">
    <Dropdown
      id="settings-defaults-thinking-effort"
      value={agentDefaults.thinking_effort}
      options={thinkingEffortOptions}
      ariaLabel={t('settings.defaults.thinkingEffort', 'Thinking effort')}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={(selectedValue) =>
        handleAgentDefaultsChange('thinking_effort', selectedValue)}
    />
  </div>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualAgentDefaultsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
