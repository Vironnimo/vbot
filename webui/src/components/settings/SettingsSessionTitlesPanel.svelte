<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import SearchableDropdown from '../SearchableDropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveButton from '../ui/SaveButton.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { listConnections, listModels } from '$lib/api.js';
  import {
    createDebouncedAutosave,
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
  let lastModelsRefreshToken = null;

  let allModelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: formValues.model,
      emptyLabel: t('settings.sessionTitles.agentModel'),
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
    }),
  );
  let saveDisabled = $derived(
    saving || sessionTitleSettingsMatch(formValues, settings),
  );
  const autosaveContext = useAutosaveContext();
  const sessionTitlesAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...formValues }),
    hasChanges: () => !sessionTitleSettingsMatch(formValues, settings),
    save,
  });
  const unregisterSessionTitlesAutosave = autosaveContext.register(
    sessionTitlesAutosave.participant,
  );

  onMount(() => void loadModelCatalogs());
  onDestroy(() => {
    unregisterSessionTitlesAutosave();
    sessionTitlesAutosave.cancelPendingTimer();
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
    sessionTitlesAutosave.scheduleRun();
    return () => sessionTitlesAutosave.cancelPendingTimer();
  });

  async function loadModelCatalogs() {
    try {
      const [modelsResult, connectionsResult] = await Promise.all([
        listModels(),
        listConnections(),
      ]);
      availableModels = modelsResult.models;
      availableConnections = connectionsResult.connections;
    } catch (error) {
      onError(`${t('settings.models.loadError')} ${error.message}`);
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

  async function save(reason) {
    if (sessionTitleSettingsMatch(formValues, settings)) return true;
    return runSettingsSave({
      reason,
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildSessionTitleSettingsPayload(formValues),
      successTitle: t('settings.sessionTitles.saveSuccess'),
      getDraftSnapshot: () => formValues,
      applyResult: (next) => (formValues = normalizeSessionTitleSettings(next)),
    });
  }

  function saveNow() {
    if (saveDisabled) {
      onToast({
        title: t('common.alreadySaved'),
        variant: 'success',
      });
      return;
    }
    sessionTitlesAutosave.cancelPendingTimer();
    void sessionTitlesAutosave.participant.runSave('manual');
  }
</script>

<!-- The Title model only matters while automatic titles are on; its draft
     stays in the form while the row is hidden. -->
<div class="s-group">
  <div class="s-row s-row--compact">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.sessionTitles.enabled')}
        <InfoHint text={t('settings.sessionTitles.enabledHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.sessionTitles.enabledDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Toggle
        checked={formValues.enabled}
        ariaLabel={t('settings.sessionTitles.enabled')}
        onChange={(enabled) => update({ enabled })}
      />
    </div>
  </div>

  <div class="s-row" hidden={!formValues.enabled}>
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.sessionTitles.model')}
        <InfoHint text={t('settings.sessionTitles.modelHelp')} />
      </div>
    </div>
    <div class="s-row-control s-row-control--model">
      <SearchableDropdown
        id="settings-session-title-model"
        value={modelSelectValue}
        options={modelOptions}
        placeholder={t('settings.sessionTitles.agentModel')}
        searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
        emptyLabel={t('agents.form.modelSearchEmpty')}
        ariaLabel={t('settings.sessionTitles.model')}
        triggerClass="settings-view__dropdown"
        panelClass="settings-view__model-panel"
        footerActionLabel={modelFilterFooter}
        onFooterAction={() => (showAllModels = !showAllModels)}
        onValueChange={selectModel}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveButton
    class="s-save-button s-save-button--inline"
    {saving}
    pending={sessionTitlesAutosave.participant.hasChanges()}
    onClick={saveNow}
  />
</div>
