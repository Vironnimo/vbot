import { formatDateTimeInApplicationZone } from '../dateTimePrefs.svelte.js';
import { activeLocaleTag, t } from '../i18n.js';

// Settings -> System -> Access & devices: labels and messages for the server's
// access mode, its Devices and pairing approval.

export const ACCESS_MODE_AUTHENTICATED = 'authenticated';

export function accessModeDescription(mode) {
  if (mode === 'local') {
    return t('settings.access.modeLocal');
  }
  if (mode === 'lan') {
    return t('settings.access.modeLan');
  }
  if (mode === ACCESS_MODE_AUTHENTICATED) {
    return t('settings.access.modeAuthenticated');
  }
  return '';
}

export function deviceKindLabel(kind) {
  if (kind === 'browser') {
    return t('settings.access.kind.browser');
  }
  if (kind === 'desktop') {
    return t('settings.access.kind.desktop');
  }
  if (kind === 'cli') {
    return t('settings.access.kind.cli');
  }
  return t('settings.access.kind.unknown');
}

export function formatDeviceTime(value) {
  const date = new Date(value ?? '');
  if (!value || Number.isNaN(date.getTime())) {
    return '';
  }
  return formatDateTimeInApplicationZone(date, activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

// One line under a Device's name: its kind, when it was added and last
// used, and the address it last connected from.
export function describeDeviceUse(device) {
  const parts = [deviceKindLabel(device?.kind)];
  const created = formatDeviceTime(device?.created_at);
  if (created) {
    parts.push(t('settings.access.devices.added', { time: created }));
  }
  const lastUsed = formatDeviceTime(device?.last_used_at);
  if (lastUsed) {
    parts.push(t('settings.access.devices.lastUsed', { time: lastUsed }));
  }
  if (device?.last_client) {
    parts.push(
      t('settings.access.devices.lastClient', { address: device.last_client }),
    );
  }
  return parts.join(' · ');
}

// The message for a failed Device request (list, rename, remove, sign out).
export function deviceErrorMessage(error) {
  if (error?.code === 'devices_unavailable') {
    return t('settings.access.error.devicesUnavailable');
  }
  if (error?.code === 'device_not_found') {
    return t('settings.access.error.deviceNotFound');
  }
  if (error?.code === 'invalid_request') {
    return `${t('settings.access.error.invalid')} ${error.message ?? ''}`.trim();
  }
  return `${t('settings.access.error.generic')} ${error?.message ?? ''}`.trim();
}

// The message for a failed look-up, approval or denial of a pairing code.
export function pairingErrorMessage(error) {
  if (error?.code === 'pairing_not_found') {
    return t('settings.access.add.notFound');
  }
  if (error?.code === 'rate_limited') {
    const seconds = Number(error.details?.data?.retry_after);
    const minutes = Number.isFinite(seconds)
      ? Math.max(1, Math.ceil(seconds / 60))
      : 10;
    return t('settings.access.add.rateLimited', { minutes });
  }
  if (error?.code === 'auth_disabled') {
    return t('settings.access.add.authDisabled');
  }
  return deviceErrorMessage(error);
}
