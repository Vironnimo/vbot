import { untrack } from 'svelte';
import { t } from './i18n.js';
import { createSettingsDraft } from './settingsSave.js';

// Mounted Settings editors receive live server projections as well as their
// own save responses. Rebase untouched fields before autosave can mistake
// the old form values for user input. A save reconciles its submitted values
// and the edits made since submission before adopting another projection.
export function createSettingsEditorDraft({
  getSettings,
  onError,
  ...options
}) {
  let observed = untrack(getSettings);
  const draft = createSettingsDraft({
    ...options,
    settings: observed,
    recoverOnReconnect: true,
  });
  let saving = $state(false);

  $effect.pre(() => {
    const settings = getSettings();
    if (saving || settings === observed) return;
    observed = settings;
    untrack(() => {
      if (draft.adopt(settings)) onError(t('settings.saveConflict'));
    });
  });

  return {
    async save(callbacks) {
      saving = true;
      try {
        return await draft.save(callbacks);
      } finally {
        saving = false;
      }
    },
  };
}
