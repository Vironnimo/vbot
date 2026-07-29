<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { t } from '$lib/i18n.js';
  import { runSettingsSave } from '$lib/settingsSave.js';
  import {
    buildRecallBackendOptions,
    buildRecallSettingsPayload,
    getRecallSettings,
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
  let recallSettings = $state(untrack(() => getRecallSettings(settings)));
  let saving = $state(false);
  let autoSaveTimer = null;

  let recallBackendOptions = $derived(
    buildRecallBackendOptions(recallSettings, t),
  );
  let saveDisabled = $derived(
    saving || recallSettingsMatch(recallSettings, getRecallSettings(settings)),
  );
  const autosaveContext = useAutosaveContext();
  const recallAutosave = createAutosaveParticipant({
    cancelPending: clearAutoSaveTimer,
    getSnapshot: () => ({ ...recallSettings }),
    hasChanges: () =>
      !recallSettingsMatch(recallSettings, getRecallSettings(settings)),
    save: saveRecallSettings,
  });
  const unregisterRecallAutosave = autosaveContext.register(recallAutosave);

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void recallAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
    unregisterRecallAutosave();
    clearAutoSaveTimer();
  });

  function clearAutoSaveTimer() {
    if (autoSaveTimer !== null) {
      clearTimeout(autoSaveTimer);
      autoSaveTimer = null;
    }
  }

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

  function handleManualRecallSettingsSave() {
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
    void recallAutosave.runSave('manual');
  }

  async function saveRecallSettings() {
    if (recallSettingsMatch(recallSettings, getRecallSettings(settings))) {
      return true;
    }

    return runSettingsSave({
      onCommit,
      onToast,
      onError,
      setSaving: (value) => (saving = value),
      buildPayload: () => buildRecallSettingsPayload(recallSettings),
      successKey: 'settings.recall.saveSuccess',
      successFallback: 'Recall backend updated.',
      getDraftSnapshot: () => recallSettings,
      applyResult: (next) => (recallSettings = getRecallSettings(next)),
    });
  }
</script>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.recall.backend', 'Recall backend')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.recall.backendDescription',
        'How the session search looks through stored conversations.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--recall">
    <Dropdown
      id="settings-recall-backend"
      value={recallSettings.backend}
      options={recallBackendOptions}
      ariaLabel={t('settings.recall.backend', 'Recall backend')}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={handleRecallBackendChange}
    />
  </div>
</div>

{#if recallSettings.backend === 'vector'}
  <div class="s-row s-row--stacked">
    <div class="s-row-info">
      <div class="s-row-desc">
        {t(
          'settings.recall.vectorHint',
          'Semantic search requires an embedding model — configure it under Specialized Models.',
        )}
      </div>
    </div>
  </div>
{/if}

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualRecallSettingsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
