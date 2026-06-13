<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import { rpc } from '$lib/api.js';
  import { init, t } from '$lib/i18n.js';
  import { setChatWidth } from '$lib/appearancePrefs.svelte.js';
  import {
    buildChatWidthOptions,
    buildLanguageOptions,
    createAppearanceUpdatePayload,
    getPersistedChatWidth,
    getPersistedLanguageId,
    isAppearanceSaveDisabled,
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
  let selectedChatWidth = $state(
    untrack(() => getPersistedChatWidth(settings)),
  );
  let saving = $state(false);
  let autoSaveTimer = null;

  let availableLanguageOptions = $derived(
    buildLanguageOptions(settings?.appearance),
  );
  let languageDropdownOptions = $derived(
    availableLanguageOptions.map((language) => ({
      value: language.id,
      label: t(language.labelKey, language.labelFallback),
    })),
  );
  let chatWidthDropdownOptions = $derived(
    buildChatWidthOptions().map((option) => ({
      value: option.id,
      label: t(option.labelKey, option.labelFallback),
    })),
  );
  let persistedLanguageId = $derived(getPersistedLanguageId(settings));
  let persistedChatWidth = $derived(getPersistedChatWidth(settings));
  let saveDisabled = $derived(
    isAppearanceSaveDisabled({
      loading: false,
      saving,
      selectedLanguageId,
      selectedChatWidth,
      persistedLanguageId,
      persistedChatWidth,
    }),
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void saveAppearance();
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

  function handleLanguageChange(value) {
    selectedLanguageId = value;
    onError('');
  }

  function handleChatWidthChange(value) {
    selectedChatWidth = value;
    onError('');
  }

  function handleManualSave() {
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
    void saveAppearance();
  }

  async function saveAppearance() {
    if (saveDisabled) {
      return;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await rpc(
        'settings.update',
        createAppearanceUpdatePayload({
          language: selectedLanguageId,
          chatWidth: selectedChatWidth,
        }),
      );
      onCommit(nextSettings);
      init(selectedLanguageId);
      // Update the app-wide prefs store so the open chat reflows live (no
      // reload), since chat width has no runtime reload hook.
      setChatWidth(selectedChatWidth);
      onToast({
        title: t('settings.appearance.saveSuccess', 'Appearance updated.'),
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
    <Dropdown
      id="settings-appearance-language"
      value={selectedLanguageId}
      options={languageDropdownOptions}
      ariaLabel={t('settings.appearance.language', 'Language')}
      disabled={saving || availableLanguageOptions.length <= 1}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={handleLanguageChange}
    />
  </div>
</div>

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.appearance.chatWidth.label', 'Chat width')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.appearance.chatWidth.description',
        'Reading width of the chat column on wide screens.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--appearance">
    <Dropdown
      id="settings-appearance-chat-width"
      value={selectedChatWidth}
      options={chatWidthDropdownOptions}
      ariaLabel={t('settings.appearance.chatWidth.label', 'Chat width')}
      disabled={saving}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={handleChatWidthChange}
    />
  </div>
</div>

<div class="s-footer">
  <button
    class="btn-primary s-save-button s-save-button--inline"
    type="button"
    onclick={handleManualSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </button>
</div>
