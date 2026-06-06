<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import { rpc } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
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

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveRecallSettings();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
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
    void saveRecallSettings();
  }

  async function saveRecallSettings() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        buildRecallSettingsPayload(recallSettings),
      );
      onCommit(nextSettings);
      recallSettings = getRecallSettings(nextSettings);
      onToast({
        title: t('settings.recall.saveSuccess', 'Recall backend updated.'),
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
      {t('settings.recall.backend', 'Recall backend')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.recall.backendDescription',
        'Backend used by session_search for stored Session recall.',
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

<div class="s-sticky-footer">
  <button
    class="btn-primary s-save-button s-save-button--inline"
    type="button"
    onclick={handleManualRecallSettingsSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('settings.recall.save', 'Save')}
  </button>
</div>
