import { updateSettings } from './api.js';
import { t } from './i18n.js';

/**
 * Run the shared settings-panel save lifecycle.
 *
 * Every settings panel's save handler is the same shape: mark saving, clear the
 * error, push the built payload through `settings.update`, commit (and
 * optionally re-seed local state from) the result, and surface any failure
 * through `onError` — always clearing the saving flag. The panel's save state
 * confirms success. This is the one home for that lifecycle and the save-error
 * message format.
 *
 * @param {object} params
 * @param {() => object} params.buildPayload - Builds the `settings.update` params.
 * @param {(next: object) => void} params.onCommit - Receives the updated settings.
 * @param {(message: string) => void} params.onError - Sets/clears the error text.
 * @param {(saving: boolean) => void} params.setSaving - Drives the panel's saving flag.
 * @param {(next: object) => void} [params.applyResult] - Optional: re-seed local state.
 * @param {() => unknown} [params.getDraftSnapshot] - Optional: reads the current local draft.
 */
export async function runSettingsSave({
  buildPayload,
  onCommit,
  onError,
  setSaving,
  applyResult,
  getDraftSnapshot,
}) {
  setSaving(true);
  onError('');

  try {
    const payload = buildPayload();
    const readDraftSnapshot = getDraftSnapshot ?? buildPayload;
    const submittedDraftSnapshot = applyResult
      ? JSON.stringify(readDraftSnapshot())
      : null;
    const nextSettings = await updateSettings(payload);
    const draftIsCurrent =
      !applyResult ||
      JSON.stringify(readDraftSnapshot()) === submittedDraftSnapshot;
    onCommit(nextSettings);
    if (draftIsCurrent) {
      applyResult?.(nextSettings);
    }
    return true;
  } catch (error) {
    onError(`${t('settings.saveError')} ${error.message}`);
    return false;
  } finally {
    setSaving(false);
  }
}
