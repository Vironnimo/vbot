<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Button from '../ui/Button.svelte';
  import { updateSettings } from '$lib/api.js';
  import {
    createAutosaveParticipant,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import { init, t } from '$lib/i18n.js';
  import {
    setChatWidth,
    setChatWorkingMode,
  } from '$lib/appearancePrefs.svelte.js';
  import {
    buildChatWidthOptions,
    buildChatWorkingModeOptions,
    buildLanguageOptions,
    createAppearanceUpdatePayload,
    getPersistedChatWidth,
    getPersistedChatWorkingMode,
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
  let selectedChatWorkingMode = $state(
    untrack(() => getPersistedChatWorkingMode(settings)),
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
  let chatWorkingModeDropdownOptions = $derived(
    buildChatWorkingModeOptions().map((option) => ({
      value: option.id,
      label: t(option.labelKey, option.labelFallback),
    })),
  );
  let persistedLanguageId = $derived(getPersistedLanguageId(settings));
  let persistedChatWidth = $derived(getPersistedChatWidth(settings));
  let persistedChatWorkingMode = $derived(
    getPersistedChatWorkingMode(settings),
  );
  let saveDisabled = $derived(
    isAppearanceSaveDisabled({
      loading: false,
      saving,
      selectedLanguageId,
      selectedChatWidth,
      selectedChatWorkingMode,
      persistedLanguageId,
      persistedChatWidth,
      persistedChatWorkingMode,
    }),
  );
  const autosaveContext = useAutosaveContext();
  const appearanceAutosave = createAutosaveParticipant({
    cancelPending: clearAutoSaveTimer,
    getSnapshot: () => ({
      language: selectedLanguageId,
      chatWidth: selectedChatWidth,
      chatWorkingMode: selectedChatWorkingMode,
    }),
    hasChanges: appearanceHasChanges,
    save: saveAppearance,
  });
  const unregisterAppearanceAutosave =
    autosaveContext.register(appearanceAutosave);

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    autoSaveTimer = setTimeout(() => {
      autoSaveTimer = null;
      void appearanceAutosave.runSave();
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAutoSaveTimer();
    };
  });

  onDestroy(() => {
    unregisterAppearanceAutosave();
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

  function handleChatWorkingModeChange(value) {
    selectedChatWorkingMode = value;
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
    void appearanceAutosave.runSave('manual');
  }

  function appearanceHasChanges() {
    return !isAppearanceSaveDisabled({
      loading: false,
      saving: false,
      selectedLanguageId,
      selectedChatWidth,
      selectedChatWorkingMode,
      persistedLanguageId,
      persistedChatWidth,
      persistedChatWorkingMode,
    });
  }

  async function saveAppearance() {
    if (!appearanceHasChanges()) {
      return true;
    }

    saving = true;
    onError('');

    try {
      const nextSettings = await updateSettings(
        createAppearanceUpdatePayload({
          language: selectedLanguageId,
          chatWidth: selectedChatWidth,
          chatWorkingMode: selectedChatWorkingMode,
        }),
      );
      onCommit(nextSettings);
      init(selectedLanguageId);
      // Update the app-wide prefs store so the open Chat changes live; these
      // display preferences have no runtime reload hook.
      setChatWidth(selectedChatWidth);
      setChatWorkingMode(selectedChatWorkingMode);
      onToast({
        title: t('settings.appearance.saveSuccess', 'Appearance updated.'),
        variant: 'success',
      });
      return true;
    } catch (error) {
      onError(
        `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
      );
      return false;
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

<div class="s-row">
  <div class="s-row-info">
    <div class="s-row-label">
      {t('settings.appearance.chatWorkingMode.label', 'Work details')}
    </div>
    <div class="s-row-desc">
      {t(
        'settings.appearance.chatWorkingMode.description',
        'Show Thinking and Tool activity inline or group it into Working blocks.',
      )}
    </div>
  </div>
  <div class="s-row-control s-row-control--appearance">
    <Dropdown
      id="settings-appearance-chat-working-mode"
      value={selectedChatWorkingMode}
      options={chatWorkingModeDropdownOptions}
      ariaLabel={t('settings.appearance.chatWorkingMode.label', 'Work details')}
      disabled={saving}
      triggerClass="settings-view__dropdown"
      listClass="settings-view__thinking-list"
      onValueChange={handleChatWorkingModeChange}
    />
  </div>
</div>

<div class="s-footer">
  <Button
    variant="primary"
    class="s-save-button s-save-button--inline"
    onClick={handleManualSave}
  >
    {saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}
  </Button>
</div>
