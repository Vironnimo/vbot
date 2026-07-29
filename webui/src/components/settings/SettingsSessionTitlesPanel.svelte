<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { listConnections, listModels } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildSessionTitleSettingsPayload,
    normalizeSessionTitleSettings,
  } from '$lib/settingsView.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
    modelsRefreshToken = 0,
  } = $props();

  let formValues = $state(
    untrack(() => normalizeSessionTitleSettings(settings)),
  );
  let saving = $state(false);
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let showAllModels = $state(false);
  let timer = null;
  let lastModelsRefreshToken = null;

  let allModelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: formValues.model,
      emptyLabel: t(
        'settings.sessionTitles.agentModel',
        'Agent Model (default)',
      ),
      translate: t,
    }),
  );
  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: formValues.model,
    }),
  );
  let modelSelectValue = $derived(
    selectModelValue(formValues.model, modelOptions),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
      translate: t,
    }),
  );
  let saveDisabled = $derived(
    saving || sessionTitleSettingsMatch(formValues, settings),
  );
  const autosaveContext = useAutosaveContext();
  const sessionTitlesAutosave = createAutosaveParticipant({
    cancelPending: clearTimer,
    getSnapshot: () => ({ ...formValues }),
    hasChanges: () => !sessionTitleSettingsMatch(formValues, settings),
    save,
  });
  const unregisterSessionTitlesAutosave = autosaveContext.register(
    sessionTitlesAutosave,
  );

  onMount(() => void loadModelCatalogs());
  onDestroy(() => {
    unregisterSessionTitlesAutosave();
    clearTimer();
  });

  $effect(() => {
    if (lastModelsRefreshToken === null) {
      lastModelsRefreshToken = modelsRefreshToken;
      return;
    }
    if (modelsRefreshToken !== lastModelsRefreshToken) {
      lastModelsRefreshToken = modelsRefreshToken;
      void loadModelCatalogs();
    }
  });

  $effect(() => {
    if (saveDisabled) return;
    timer = setTimeout(() => {
      timer = null;
      void sessionTitlesAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);
    return clearTimer;
  });

  function clearTimer() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  async function loadModelCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        listModels(),
        listConnections(),
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

  function sessionTitleSettingsMatch(left, right) {
    const normalizedLeft = normalizeSessionTitleSettings(left);
    const normalizedRight = normalizeSessionTitleSettings(right);
    return (
      normalizedLeft.enabled === normalizedRight.enabled &&
      normalizedLeft.model === normalizedRight.model
    );
  }

  function update(next) {
    formValues = { ...formValues, ...next };
    onError('');
  }

  function selectModel(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    update({
      model: modelSelectionValue(selection.model, selection.connectionLocalId),
    });
  }

  async function save() {
    if (sessionTitleSettingsMatch(formValues, settings)) return true;
    return runSettingsSave({
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildSessionTitleSettingsPayload(formValues),
      successKey: 'settings.sessionTitles.saveSuccess',
      successFallback: 'Session title settings updated.',
      getDraftSnapshot: () => formValues,
      applyResult: (next) => (formValues = normalizeSessionTitleSettings(next)),
    });
  }

  function saveNow() {
    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved', 'Already saved'),
        variant: 'success',
      });
      return;
    }
    clearTimer();
    void sessionTitlesAutosave.runSave('manual');
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.sessionTitles.enabled', 'Automatic Session titles')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.sessionTitles.enabledDescription',
        'Creates one additional Model request from a bounded excerpt of the first message in each new Session. When off, the first 40 normalized characters remain as the local title.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <Toggle
      checked={formValues.enabled}
      ariaLabel={t(
        'settings.sessionTitles.enabled',
        'Automatic Session titles',
      )}
      onChange={(enabled) => update({ enabled })}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.sessionTitles.model', 'Title Model')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.sessionTitles.modelDescription',
        'Uses the active Agent Model when no separate Model is selected. A failed request keeps the local title and never triggers another paid Model request.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--model">
    <SearchableDropdown
      id="settings-session-title-model"
      value={modelSelectValue}
      options={modelOptions}
      disabled={!formValues.enabled}
      placeholder={t(
        'settings.sessionTitles.agentModel',
        'Agent Model (default)',
      )}
      searchPlaceholder={t(
        'agents.form.modelSearchPlaceholder',
        'Filter models…',
      )}
      emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
      ariaLabel={t('settings.sessionTitles.model', 'Title Model')}
      triggerClass="settings-view__dropdown"
      panelClass="settings-view__model-panel"
      footerActionLabel={modelFilterFooter}
      onFooterAction={() => (showAllModels = !showAllModels)}
      onValueChange={selectModel}
    />
  </div>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={saveNow}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
