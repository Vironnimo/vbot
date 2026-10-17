import { t } from '../i18n.js';

// The Secret Store problem that the Settings pages managing credentials show,
// from the `secrets` projection of `data_store.status`; '' when there is none.
export function describeSecretStoreProblem(secretStore) {
  if (secretStore?.state === 'unavailable') {
    return t('settings.secretStore.unavailable');
  }
  const damaged = Array.isArray(secretStore?.damaged)
    ? secretStore.damaged
    : [];
  if (damaged.length > 0) {
    // Entries are named `<section>/<name>`; the name is what the user set.
    return t('settings.secretStore.damaged', {
      names: damaged.map((entry) => String(entry).split('/').pop()).join(', '),
    });
  }
  return '';
}
