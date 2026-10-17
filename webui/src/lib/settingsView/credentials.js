import { t } from '../i18n.js';

// Settings -> Integrations -> Credentials: the named credentials of the
// server's Secret Store as `secrets.list` reports them (names, states and
// sources, never values), and the server's rules for a new one.

const CREDENTIAL_NAME_MAX_LENGTH = 200;
// An environment-variable name, the server's `ENV_KEY_PATTERN`
// (core/storage/storage.py).
const CREDENTIAL_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

export const CREDENTIAL_SOURCE_ENVIRONMENT = 'process_environment';
const CREDENTIAL_SOURCE_STORED = 'data_dir';
export const CREDENTIAL_STORE_UNAVAILABLE = 'unavailable';

export function normalizeCredentialList(result) {
  const entries = Array.isArray(result?.credentials) ? result.credentials : [];
  return {
    state: typeof result?.state === 'string' ? result.state : '',
    reason: typeof result?.reason === 'string' ? result.reason : '',
    credentials: entries
      .filter((entry) => typeof entry?.name === 'string' && entry.name)
      .map((entry) => ({
        name: entry.name,
        configured: entry.configured === true,
        source:
          entry.source === CREDENTIAL_SOURCE_ENVIRONMENT ||
          entry.source === CREDENTIAL_SOURCE_STORED
            ? entry.source
            : null,
        damaged: entry.damaged === true,
      })),
  };
}

// Why the server would refuse a typed name, '' when it accepts it. A name
// already listed is refused too: its row replaces the value.
export function credentialNameProblem(name, credentials = []) {
  if (!name) {
    return t('settings.credentials.nameRequired');
  }
  if (name.length > CREDENTIAL_NAME_MAX_LENGTH) {
    return t('settings.credentials.nameTooLong', {
      max: CREDENTIAL_NAME_MAX_LENGTH,
    });
  }
  if (!CREDENTIAL_NAME_PATTERN.test(name)) {
    return t('settings.credentials.nameInvalid');
  }
  if (credentials.some((credential) => credential.name === name)) {
    return t('settings.credentials.nameTaken');
  }
  return '';
}

// A blank value would be saved but count as missing.
export function credentialValueProblem(value) {
  return String(value ?? '').trim()
    ? ''
    : t('settings.credentials.valueRequired');
}

// What a credential's row shows: one short line, a chip only while it needs
// attention, and whether vBot can change it. A value in the server's process
// environment takes precedence over the saved one and changes only there.
export function describeCredential(credential) {
  const fromEnvironment = credential.source === CREDENTIAL_SOURCE_ENVIRONMENT;
  let chip = null;
  if (credential.damaged) {
    chip = { label: t('settings.credentials.chipDamaged'), variant: 'error' };
  } else if (!credential.configured) {
    chip = { label: t('settings.credentials.chipMissing'), variant: 'warn' };
  }
  let description;
  if (fromEnvironment) {
    description = credential.configured
      ? t('settings.credentials.environment')
      : t('settings.credentials.environmentEmpty');
  } else if (credential.damaged) {
    description = t('settings.credentials.damaged');
  } else if (credential.source === CREDENTIAL_SOURCE_STORED) {
    description = t('settings.credentials.stored');
  } else {
    description = t('settings.credentials.unreadable');
  }
  return { description, chip, editable: !fromEnvironment };
}
