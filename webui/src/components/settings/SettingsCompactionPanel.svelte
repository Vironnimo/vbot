<script>
  import { onDestroy, onMount, untrack } from 'svelte';

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
    buildCompactionSettingsPayload,
    getCompactionSettings,
    normalizeCompactionSettings,
  } from '$lib/settingsView.js';

  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
  } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let compactionSettings = $state(
    untrack(() => getCompactionSettings(settings)),
  );
  let saving = $state(false);
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let autoSaveTimer = null;

  let compactionSummaryModelOptions = $derived(
    selectModelOptions(
      compactionSettings.summary_model ?? '',
      t('settings.compaction.summaryModelPlaceholder', 'Active agent model'),
    ),
  );
  let compactionSummaryModelSelectValue = $derived(
    selectModelValue(
      compactionSettings.summary_model ?? '',
      compactionSummaryModelOptions,
    ),
  );
  let saveDisabled = $derived(
    saving ||
      compactionSettingsMatch(
        compactionSettings,
        getCompactionSettings(settings),
      ),
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
      void saveCompactionSettings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

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

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

  function compactionSettingsMatch(left, right) {
    const normalizedLeft = normalizeCompactionSettings({ compaction: left });
    const normalizedRight = normalizeCompactionSettings({ compaction: right });

    return (
      normalizedLeft.auto === normalizedRight.auto &&
      normalizedLeft.threshold === normalizedRight.threshold &&
      normalizedLeft.tail_tokens === normalizedRight.tail_tokens &&
      normalizedLeft.summary_model === normalizedRight.summary_model
    );
  }

  function handleCompactionSettingChange(key, value) {
    compactionSettings = {
      ...compactionSettings,
      [key]: value,
    };
    onError('');
  }

  function updateCompactionSummaryModelSelection(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    handleCompactionSettingChange(
      'summary_model',
      modelSelectionValue(selection.model, selection.connectionLocalId),
    );
  }

  function handleManualCompactionSettingsSave() {
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
    void saveCompactionSettings();
  }

  async function saveCompactionSettings() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildCompactionSettingsPayload(compactionSettings),
      );
      onCommit(nextSettings);
      onToast({
        title: t('settings.compaction.saved', 'Compaction settings saved.'),
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
      {t('settings.compaction.auto', 'Auto-compact')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.compaction.autoDescription',
        'Automatically compact when the context threshold is reached.',
      )}
    </div>
  </div>
  <div class="s-row-control">
    <button
      class="toggle"
      class:on={compactionSettings.auto === true}
      type="button"
      role="switch"
      aria-checked={compactionSettings.auto === true}
      aria-label={t('settings.compaction.auto', 'Auto-compact')}
      onclick={() =>
        handleCompactionSettingChange('auto', compactionSettings.auto !== true)}
    >
      <span class="t-knob"></span>
    </button>
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.compaction.threshold', 'Threshold')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.compaction.thresholdDescription',
        'Compact when context usage exceeds this fraction (0–1).',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <input
      class="s-input"
      type="text"
      inputmode="decimal"
      value={compactionSettings.threshold}
      aria-label={t('settings.compaction.threshold', 'Threshold')}
      oninput={(event) =>
        handleCompactionSettingChange('threshold', event.currentTarget.value)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.compaction.tailTokens', 'Tail tokens')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.compaction.tailTokensDescription',
        'Number of tokens preserved verbatim at the end of context.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--number">
    <input
      class="s-input"
      type="number"
      min="1"
      step="1000"
      value={compactionSettings.tail_tokens}
      aria-label={t('settings.compaction.tailTokens', 'Tail tokens')}
      oninput={(event) =>
        handleCompactionSettingChange('tail_tokens', event.currentTarget.value)}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.compaction.summaryModel', 'Summary model')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.compaction.summaryModelDescription',
        'Model used for summarization. Leave blank to use the active agent model.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--model">
    <SearchableDropdown
      id="settings-compaction-summary-model"
      value={compactionSummaryModelSelectValue}
      options={compactionSummaryModelOptions}
      placeholder={t(
        'settings.compaction.summaryModelPlaceholder',
        'Active agent model',
      )}
      searchPlaceholder={t(
        'agents.form.modelSearchPlaceholder',
        'Filter models…',
      )}
      emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
      ariaLabel={t('settings.compaction.summaryModel', 'Summary model')}
      triggerClass="settings-view__dropdown"
      panelClass="settings-view__model-panel"
      onValueChange={updateCompactionSummaryModelSelection}
    />
  </div>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualCompactionSettingsSave}
  >
    {saving
      ? t('common.saving', 'Saving…')
      : t('settings.compaction.save', 'Save')}
  </Button>
</div>
