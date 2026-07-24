import { updateSettings } from './api.js';
import { t } from './i18n.js';

/**
 * Run the shared settings-panel save lifecycle.
 *
 * Every settings panel's save handler is the same shape: mark saving, clear the
 * error, push the built payload through `settings.update`, commit (and
 * optionally re-seed local state from) the result, toast success, and surface
 * any failure through `onError` — always clearing the saving flag. This is the
 * one home for that lifecycle, the success-toast shape, and the save-error
 * message format.
 *
 * @param {object} params
 * @param {() => object} params.buildPayload - Builds the `settings.update` params.
 * @param {(next: object) => void} params.onCommit - Receives the updated settings.
 * @param {(toast: object) => void} params.onToast - Shows a toast.
 * @param {(message: string) => void} params.onError - Sets/clears the error text.
 * @param {(saving: boolean) => void} params.setSaving - Drives the panel's saving flag.
 * @param {string} params.successKey - i18n key for the success toast title.
 * @param {string} params.successFallback - English fallback for the success title.
 * @param {(next: object) => void} [params.applyResult] - Optional: re-seed local state.
 */
export async function runSettingsSave({
  buildPayload,
  onCommit,
  onToast,
  onError,
  setSaving,
  successKey,
  successFallback,
  applyResult,
}) {
  setSaving(true);
  onError('');

  try {
    const nextSettings = await updateSettings(buildPayload());
    onCommit(nextSettings);
    applyResult?.(nextSettings);
    onToast({ title: t(successKey, successFallback), variant: 'success' });
    return true;
  } catch (error) {
    onError(
      `${t('settings.saveError', 'Settings could not be saved.')} ${error.message}`,
    );
    return false;
  } finally {
    setSaving(false);
  }
}
