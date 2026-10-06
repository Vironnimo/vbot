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
  import { createSettingsDraft } from '$lib/settingsSave.js';
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

  const noop = () => {};

  let { settings = null, onCommit = noop, onError = noop } = $props();

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
  const appearanceDraft = createSettingsDraft({
    settings: untrack(() => settings),
    fromSettings: (next) => ({
      language: next?.appearance?.language ?? 'en',
      chatWidth: getPersistedChatWidth(next),
      chatWorkingMode: getPersistedChatWorkingMode(next),
    }),
    read: () => ({
      language: selectedLanguageId,
      chatWidth: selectedChatWidth,
      chatWorkingMode: selectedChatWorkingMode,
    }),
    write: (next) => {
      selectedLanguageId = next.language;
      selectedChatWidth = next.chatWidth;
      selectedChatWorkingMode = next.chatWorkingMode;
    },
    toPayload: createAppearanceUpdatePayload,
  });

  let availableLanguageOptions = $derived(
    buildLanguageOptions(settings?.appearance),
  );
  let languageDropdownOptions = $derived(
    availableLanguageOptions.map((language) => ({
      value: language.id,
      label: language.label,
    })),
  );
  let chatWidthDropdownOptions = $derived(
    buildChatWidthOptions().map((option) => ({
      value: option.id,
      label: option.label,
    })),
  );
  let chatWorkingModeDropdownOptions = $derived(
    buildChatWorkingModeOptions().map((option) => ({
      value: option.id,
      label: option.label,
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
  const appearanceAutosave = createDebouncedAutosave({
    getSnapshot: () => ({
      language: selectedLanguageId,
      chatWidth: selectedChatWidth,
      chatWorkingMode: selectedChatWorkingMode,
    }),
    hasChanges: appearanceHasChanges,
    save: saveAppearance,
  });
  const unregisterAppearanceAutosave = autosaveContext.register(
    appearanceAutosave.participant,
  );

  $effect(() => {
    if (saveDisabled) {
      return;
    }

    appearanceAutosave.scheduleRun();

    return () => {
      appearanceAutosave.cancelPendingTimer();
    };
  });

  onDestroy(() => {
    unregisterAppearanceAutosave();
    appearanceAutosave.cancelPendingTimer();
  });

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

    return appearanceDraft.save({
      onCommit,
      onError,
      setSaving: (value) => (saving = value),
    });
  }
</script>

<!-- The Language row appears only when there is a language to choose. -->
<div class="s-group">
  {#if availableLanguageOptions.length > 1}
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.appearance.language')}
        </div>
      </div>
      <div class="s-row-control s-row-control--appearance">
        <Dropdown
          id="settings-appearance-language"
          value={selectedLanguageId}
          options={languageDropdownOptions}
          ariaLabel={t('settings.appearance.language')}
          triggerClass="settings-view__dropdown"
          listClass="settings-view__thinking-list"
          onValueChange={handleLanguageChange}
        />
      </div>
    </div>
  {/if}

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.appearance.chatWidth.label')}
      </div>
      <div class="s-row-desc">
        {t('settings.appearance.chatWidth.description')}
      </div>
    </div>
    <div class="s-row-control s-row-control--appearance">
      <Dropdown
        id="settings-appearance-chat-width"
        value={selectedChatWidth}
        options={chatWidthDropdownOptions}
        ariaLabel={t('settings.appearance.chatWidth.label')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={handleChatWidthChange}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.appearance.chatWorkingMode.label')}
        <InfoHint text={t('settings.appearance.chatWorkingMode.help')} />
      </div>
      <div class="s-row-desc">
        {t('settings.appearance.chatWorkingMode.description')}
      </div>
    </div>
    <div class="s-row-control s-row-control--appearance">
      <Dropdown
        id="settings-appearance-chat-working-mode"
        value={selectedChatWorkingMode}
        options={chatWorkingModeDropdownOptions}
        ariaLabel={t('settings.appearance.chatWorkingMode.label')}
        triggerClass="settings-view__dropdown"
        listClass="settings-view__thinking-list"
        onValueChange={handleChatWorkingModeChange}
      />
    </div>
  </div>
</div>

<div class="s-footer">
  <SaveStatus
    {saving}
    pending={appearanceAutosave.participant.hasChanges()}
    onClick={() => appearanceAutosave.participant.runSave('manual')}
  />
</div>
