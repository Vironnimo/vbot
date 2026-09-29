<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { listConnections, listModels } from '$lib/api.js';
  import {
    createDebouncedAutosave,
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
  import { shouldApplyReloadNow } from '$lib/resourceInvalidation.js';

  const noop = () => {};
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
      fallback_models: normalized.fallback_models,
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
  // A live model reload fetches in the background but holds the visible option
  // swap while a picker is open, so an open selection is never disturbed; the
  // form's selected value lives in `agentDefaults`, separate from these options.
  let modelDropdownOpenCount = $state(0);
  let pendingModelCatalogs = null;
  let lastModelsRefreshToken = null;

  let showAllModels = $state(false);
  let allDefaultModelOptions = $derived(
    selectModelOptions(
      agentDefaults.model,
      t('settings.defaults.noModelDefault'),
    ),
  );
  let defaultModelOptions = $derived(
    filterModelSelectOptions(allDefaultModelOptions, {
      showAll: showAllModels,
      selectedModelValue: agentDefaults.model,
    }),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allDefaultModelOptions.length - defaultModelOptions.length,
    }),
  );
  let defaultModelSelectValue = $derived(
    selectModelValue(agentDefaults.model, defaultModelOptions),
  );
  // The fallback chain is an ordered list; every row shares one unfiltered
  // option catalog (a chain is short — max 5 — so no per-row filtering).
  const MAX_FALLBACK_MODEL_ROWS = 5;
  let allFallbackModelOptions = $derived(
    selectModelOptions('', t('settings.defaults.noFallbackModelDefault')),
  );
  let fallbackModelRows = $derived(
    agentDefaults.fallback_models.map((binding) => ({
      binding,
      selectValue: selectModelValue(binding, allFallbackModelOptions),
    })),
  );
  let canAddFallbackModelRow = $derived(
    agentDefaults.fallback_models.length < MAX_FALLBACK_MODEL_ROWS,
  );
  let thinkingEffortOptions = $derived([
    {
      value: AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
      label: t('settings.defaults.noThinkingEffort'),
    },
    {
      value: '',
      label: t('settings.defaults.providerThinkingEffortDefault'),
    },
    ...AGENT_THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label: t(`agents.form.thinkingEffortOption.${option}`),
    })),
  ]);
  let saveDisabled = $derived(saving || !agentDefaultsDraftHasChanges());
  const autosaveContext = useAutosaveContext();
  const modelCatalogLoader = createModelCatalogLoader({
    listModels,
    listConnections,
  });
  const agentDefaultsAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...agentDefaults }),
    hasChanges: agentDefaultsDraftHasChanges,
    save: saveAgentDefaults,
  });
  const unregisterAgentDefaultsAutosave = autosaveContext.register(
    agentDefaultsAutosave.participant,
  );

  onMount(() => {
    void loadModelCatalogs();
  });

  onDestroy(() => {
    modelCatalogLoader.invalidate();
    unregisterAgentDefaultsAutosave();
    agentDefaultsAutosave.cancelPendingTimer();
  });

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    agentDefaultsAutosave.scheduleRun();

    return () => {
      agentDefaultsAutosave.cancelPendingTimer();
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
      onError(`${t('settings.models.loadError')} ${error.message}`);
    }
  }

  async function reloadModelCatalogs() {
    pendingModelCatalogs = null;
    let catalogs;
    try {
      catalogs = await modelCatalogLoader.load();
    } catch (error) {
      onError(`${t('settings.models.loadError')} ${error.message}`);
      return;
    }
    if (catalogs === null) {
      return;
    }
    if (
      shouldApplyReloadNow({
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
    });
  }

  function agentDefaultsMatch(left, right) {
    const normalizedLeft = normalizeAgentDefaultsSettings(left);
    const normalizedRight = normalizeAgentDefaultsSettings(right);

    return (
      normalizedLeft.model === normalizedRight.model &&
      JSON.stringify(normalizedLeft.fallback_models) ===
        JSON.stringify(normalizedRight.fallback_models) &&
      normalizedLeft.temperature === normalizedRight.temperature &&
      normalizedLeft.thinking_effort === normalizedRight.thinking_effort
    );
  }

  // Dirty state, scheduling and saving all compare the normalized draft (the
  // payload that would be sent) with the persisted values, so an empty
  // fallback row or a respelled temperature that normalizes to the stored
  // value is not a pending change.
  function agentDefaultsDraftHasChanges() {
    return !agentDefaultsMatch(agentDefaults, settings);
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

  function updateFallbackModelEntry(index, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    const next = [...agentDefaults.fallback_models];
    next[index] = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );
    handleAgentDefaultsChange('fallback_models', next);
  }

  function addFallbackModelEntry() {
    handleAgentDefaultsChange('fallback_models', [
      ...agentDefaults.fallback_models,
      '',
    ]);
  }

  function removeFallbackModelEntry(index) {
    handleAgentDefaultsChange(
      'fallback_models',
      agentDefaults.fallback_models.filter(
        (_, entryIndex) => entryIndex !== index,
      ),
    );
  }

  function handleManualAgentDefaultsSave() {
    if (saving) {
      return;
    }

    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved'),
        variant: 'success',
      });
      return;
    }

    agentDefaultsAutosave.cancelPendingTimer();
    void agentDefaultsAutosave.participant.runSave('manual');
  }

  async function saveAgentDefaults(reason) {
    if (!agentDefaultsDraftHasChanges()) {
      return true;
    }

    return runSettingsSave({
      reason,
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildAgentDefaultsPayload(agentDefaults),
      successTitle: t('settings.defaults.saveSuccess'),
      getDraftSnapshot: () => agentDefaults,
      applyResult: (next) =>
        (agentDefaults = normalizeAgentDefaultsFormValues(next)),
    });
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.defaults.model')}
      </div>
      <div class="s-row-desc">
        {t('settings.defaults.modelDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--model">
      <SearchableDropdown
        id="settings-defaults-model"
        value={defaultModelSelectValue}
        options={defaultModelOptions}
        placeholder={t('settings.defaults.noModelDefault')}
        searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
        emptyLabel={t('agents.form.modelSearchEmpty')}
        ariaLabel={t('settings.defaults.model')}
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
        {t('settings.defaults.thinkingEffort')}
        <InfoHint text={t('settings.defaults.thinkingEffortHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.defaults.thinkingEffortDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--model">
      <Dropdown
        id="settings-defaults-thinking-effort"
        value={agentDefaults.thinking_effort}
        options={thinkingEffortOptions}
        ariaLabel={t('settings.defaults.thinkingEffort')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={(selectedValue) =>
          handleAgentDefaultsChange('thinking_effort', selectedValue)}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.defaults.temperature')}
        <InfoHint text={t('settings.defaults.temperatureHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.defaults.temperatureDescription')}
      </div>
    </div>
    <div class="s-row-control s-row-control--number">
      <TextField
        id="settings-defaults-temperature"
        inputmode="decimal"
        value={agentDefaults.temperature}
        ariaLabel={t('settings.defaults.temperature')}
        onInput={(next) => handleAgentDefaultsChange('temperature', next)}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.defaults.fallbackModels')}
        <InfoHint text={t('agents.form.fallbackModelsHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.defaults.fallbackModelDescription')}
      </div>
    </div>
    <div class="s-row-control agent-defaults-fallbacks">
      {#each fallbackModelRows as row, index (index)}
        <div class="settings-view__fallback-row">
          <SearchableDropdown
            id={`settings-defaults-fallback-model-${index}`}
            value={row.selectValue}
            options={allFallbackModelOptions}
            placeholder={t('settings.defaults.noFallbackModelDefault')}
            searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
            emptyLabel={t('agents.form.modelSearchEmpty')}
            ariaLabel={`${t('settings.defaults.fallbackModels')} ${index + 1}`}
            triggerClass="settings-view__dropdown"
            panelClass="settings-view__model-panel"
            onOpenChange={trackModelDropdownOpen}
            onValueChange={(selectedValue) =>
              updateFallbackModelEntry(index, selectedValue)}
          />
          <button
            type="button"
            class="settings-view__fallback-remove"
            aria-label={t('agents.form.removeFallbackModel')}
            use:tooltip={t('agents.form.removeFallbackModel')}
            onclick={() => removeFallbackModelEntry(index)}
          >
            ×
          </button>
        </div>
      {/each}
      {#if canAddFallbackModelRow}
        <button
          type="button"
          class="settings-view__fallback-add"
          onclick={addFallbackModelEntry}
        >
          {t('agents.form.addFallbackModel')}
        </button>
      {/if}
    </div>
  </div>
</div>
<div class="s-footer">
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={agentDefaultsAutosave.participant.hasChanges()}
    onClick={handleManualAgentDefaultsSave}
  />
</div>
