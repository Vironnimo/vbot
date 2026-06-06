<script>
  import { onDestroy, untrack } from 'svelte';

  import { rpc } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import {
    buildLanguageOptions,
    createLanguageUpdatePayload,
    getPersistedLanguageId,
    isLanguageSaveDisabled,
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
  let selectedLanguageId = $state(
    untrack(() => settings?.appearance?.language ?? 'en'),
  );
  let saving = $state(false);
  let autoSaveTimer = null;

  let availableLanguageOptions = $derived(
    buildLanguageOptions(settings?.appearance),
  );
  let persistedLanguageId = $derived(getPersistedLanguageId(settings));
  let saveDisabled = $derived(
    isLanguageSaveDisabled({
      loading: false,
      saving,
      selectedLanguageId,
      persistedLanguageId,
    }),
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveLanguage();
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

  function handleLanguageChange(event) {
    selectedLanguageId = event.currentTarget.value;
    onError('');
  }

  function handleManualLanguageSave() {
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
    void saveLanguage();
  }

  async function saveLanguage() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        createLanguageUpdatePayload(selectedLanguageId),
      );
      onCommit(nextSettings);
      init(selectedLanguageId);
      onToast({
        title: t(
          'settings.appearance.saveSuccess',
          'Language preference updated.',
        ),
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
      {t('settings.appearance.language', 'Language')}
    </div>
    <div class="s-row-desc">
      {t('settings.appearance.languageDescription', 'Interface language.')}
    </div>
  </div>
  <div class="s-row-control s-row-control--appearance">
    <select
      bind:value={selectedLanguageId}
      class="s-select"
      aria-label={t('settings.appearance.language', 'Language')}
      disabled={saving || availableLanguageOptions.length <= 1}
      onchange={handleLanguageChange}
    >
      {#each availableLanguageOptions as language (language.id)}
        <option value={language.id}>
          {t(language.labelKey, language.labelFallback)}
        </option>
      {/each}
    </select>
  </div>
</div>

<div class="s-sticky-footer">
  <button
    class="btn-primary s-save-button s-save-button--inline"
    type="button"
    onclick={handleManualLanguageSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </button>
</div>
