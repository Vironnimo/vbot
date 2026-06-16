<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import {
    AGENT_DEFAULTS_THINKING_EFFORT_NO_DEFAULT,
    buildAgentDefaultsPayload,
    normalizeAgentDefaultsSettings,
  } from '$lib/settingsView.js';

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

  let defaultModelOptions = $derived(
    selectModelOptions(
      agentDefaults.model,
      t('settings.defaults.noModelDefault', '— (no default)'),
    ),
  );
  let defaultFallbackModelOptions = $derived(
    selectModelOptions(
      agentDefaults.fallback_model,
      t('settings.defaults.noFallbackModelDefault', '— (no default)'),
    ),
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

  onMount(() => {
    void loadModelCatalogs();
  });

  onDestroy(() => {
    clearAutoSaveTimer();
  });

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveAgentDefaults();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  async function loadModelCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        rpc('model.list'),
        rpc('connection.list'),
      ]);

      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
      availableConnections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
    } catch (error) {
      onError(
        `${t('settings.models.loadError', 'Model catalog could not be loaded.')} ${error.message}`,
      );
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
    void saveAgentDefaults();
  }

  async function saveAgentDefaults() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildAgentDefaultsPayload(agentDefaults),
      );
      onCommit(nextSettings);
      agentDefaults = normalizeAgentDefaultsFormValues(nextSettings);
      onToast({
        title: t('settings.defaults.saveSuccess', 'Agent defaults updated.'),
        variant: 'success',
      });
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
    } finally {
      saving = false;
    }
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
      onValueChange={(selectedValue) =>
        updateAgentDefaultsModelSelection('model', selectedValue)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.fallbackModel', 'Fallback model')}
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
      onValueChange={(selectedValue) =>
        updateAgentDefaultsModelSelection('fallback_model', selectedValue)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.temperature', 'Temperature')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.defaults.temperatureDescription',
        'Used when an agent temperature is unset.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <input
      id="settings-defaults-temperature"
      class="s-input"
      type="text"
      inputmode="decimal"
      value={agentDefaults.temperature}
      aria-label={t('settings.defaults.temperature', 'Temperature')}
      oninput={(event) =>
        handleAgentDefaultsChange('temperature', event.currentTarget.value)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.defaults.thinkingEffort', 'Thinking effort')}
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
