<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildRecallBackendOptions,
    buildRecallSettingsPayload,
    getRecallSettings,
  } from '$lib/settingsView.js';
  import { normalizeTaskModelSettings } from '$lib/taskModelSettings.js';

  // Backends that rank by meaning and therefore embed conversation text.
  const EMBEDDING_BACKENDS = new Set(['vector', 'hybrid']);

  const noop = () => {};

  let { settings = null, onCommit = noop, onError = noop } = $props();

  // Form is seeded once from the settings prop at mount (untrack avoids a
  // reactive dependency); later commits flow back through saveDisabled.
  let recallSettings = $state(untrack(() => getRecallSettings(settings)));
  let saving = $state(false);

  let recallBackendOptions = $derived(
    buildRecallBackendOptions(recallSettings),
  );
  let usesEmbeddings = $derived(EMBEDDING_BACKENDS.has(recallSettings.backend));
  // The saved embedding binding (edited in the Embedding model section).
  let embeddingConfigured = $derived(
    Boolean(normalizeTaskModelSettings(settings).text_embedding?.target),
  );
  let saveDisabled = $derived(
    saving || recallSettingsMatch(recallSettings, getRecallSettings(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const recallAutosave = createDebouncedAutosave({
    getSnapshot: () => ({ ...recallSettings }),
    hasChanges: () =>
      !recallSettingsMatch(recallSettings, getRecallSettings(settings)),
    save: saveRecallSettings,
  });
  const unregisterRecallAutosave = autosaveContext.register(
    recallAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    recallAutosave.scheduleRun();

    return () => {
      recallAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterRecallAutosave();
    recallAutosave.cancelPendingTimer();
  });

  function recallSettingsMatch(left, right) {
    return (
      getRecallSettings({ recall: left }).backend ===
      getRecallSettings({ recall: right }).backend
    );
  }

  function handleRecallBackendChange(backend) {
    recallSettings = {
      ...recallSettings,
      backend,
    };
    onError('');
  }

  async function saveRecallSettings() {
    if (recallSettingsMatch(recallSettings, getRecallSettings(settings))) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildRecallSettingsPayload(recallSettings),
      getDraftSnapshot: () => recallSettings,
      applyResult: (next) => (recallSettings = getRecallSettings(next)),
    });
  }
</script>

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.recall.backend')}
        <InfoHint text={t('settings.recall.backendHelp')} />
      </div>
      <div class="s-row-desc">
        {t('settings.recall.backendDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        id="settings-recall-backend"
        value={recallSettings.backend}
        options={recallBackendOptions}
        ariaLabel={t('settings.recall.backend')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={handleRecallBackendChange}
      />
    </div>
  </div>

  {#if usesEmbeddings}
    <div
      class="s-group__block s-group__block--attached s-group__note"
      class:recall-note--attention={!embeddingConfigured}
      data-recall-embedding-note={embeddingConfigured ? 'in-use' : 'missing'}
    >
      <p>
        {embeddingConfigured
          ? t('settings.recall.embeddingInUse')
          : t('settings.recall.embeddingMissing')}
      </p>
    </div>
  {/if}
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={recallAutosave.participant.hasChanges()}
    onClick={() => recallAutosave.participant.runSave('manual')}
  />
</div>

<style>
  /* The chosen method cannot work until an embedding model is set. */
  .recall-note--attention {
    color: var(--amber);
  }
</style>
