<script>
  import { onDestroy, onMount, untrack } from 'svelte';

  import CompactionPolicyEditor from '../compaction/CompactionPolicyEditor.svelte';
  import Button from '../ui/Button.svelte';
  import { listConnections, listModels } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import {
    compactionPoliciesEqual,
    normalizeCompactionPolicy,
  } from '$lib/compactionPolicy.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildModelSelectOptions,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';

  const noop = () => {};

  let {
    settings = null,
    onCommit = noop,
    onToast = noop,
    onError = noop,
    modelsRefreshToken = 0,
  } = $props();

  let policy = $state(
    untrack(() => normalizeCompactionPolicy(settings?.compaction)),
  );
  let saving = $state(false);
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let lastModelsRefreshToken = null;
  let summaryModelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: policy.strategy.summary_model ?? '',
      emptyLabel: t(
        'settings.compaction.summaryModelPlaceholder',
        'Active agent model',
      ),
      translate: t,
    }),
  );
  let summaryModelSelectValue = $derived(
    selectModelValue(policy.strategy.summary_model ?? '', summaryModelOptions),
  );
  let saveDisabled = $derived(
    saving || compactionPoliciesEqual(policy, settings?.compaction),
  );
  const autosaveContext = useAutosaveContext();
  const compactionAutosave = createDebouncedAutosave({
    getSnapshot: () => normalizeCompactionPolicy(policy),
    hasChanges: () => !compactionPoliciesEqual(policy, settings?.compaction),
    save,
  });
  const unregisterCompactionAutosave = autosaveContext.register(
    compactionAutosave.participant,
  );

  onDestroy(() => {
    unregisterCompactionAutosave();
    compactionAutosave.cancelPendingTimer();
  });
  onMount(() => void loadModelCatalogs());

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
    compactionAutosave.scheduleRun();
    return () => compactionAutosave.cancelPendingTimer();
  });

  function update(next) {
    policy = next;
    onError('');
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

  function selectSummaryModel(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    update({
      ...policy,
      strategy: {
        ...policy.strategy,
        summary_model: modelSelectionValue(
          selection.model,
          selection.connectionLocalId,
        ),
      },
    });
  }

  async function save() {
    if (compactionPoliciesEqual(policy, settings?.compaction)) return true;
    return runSettingsSave({
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => ({ compaction: normalizeCompactionPolicy(policy) }),
      successKey: 'settings.compaction.saved',
      successFallback: 'Compaction Policy saved.',
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
    compactionAutosave.cancelPendingTimer();
    void compactionAutosave.participant.runSave('manual');
  }
</script>

<CompactionPolicyEditor
  value={policy}
  onChange={update}
  idPrefix="settings-compaction"
  {summaryModelOptions}
  {summaryModelSelectValue}
  onSummaryModelSelect={selectSummaryModel}
/>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={saveNow}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
