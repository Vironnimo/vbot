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

// Who approved a Device: `approved_by` is `host` or the id of the approving
// Device, named from `devices` while it is still paired.
function describeDeviceApprover(approvedBy, devices = []) {
  if (approvedBy === 'host') {
    return t('settings.access.devices.approvedByHost');
  }
  if (typeof approvedBy !== 'string' || !approvedBy) {
    return '';
  }
  const approver = devices.find((entry) => entry?.id === approvedBy);
  return approver?.name
    ? t('settings.access.devices.approvedBy', { name: approver.name })
    : t('settings.access.devices.approvedByRemoved');
}

// One line under a Device's name: its kind, when it was added and by whom,
// when it was last used, and the address it last connected from.
export function describeDeviceUse(device, devices = []) {
  const parts = [deviceKindLabel(device?.kind)];
  const created = formatDeviceTime(device?.created_at);
  if (created) {
    parts.push(t('settings.access.devices.added', { time: created }));
  }
  const approver = describeDeviceApprover(device?.approved_by, devices);
  if (approver) {
    parts.push(approver);
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

// Describe the known cascade for confirmation; the server still determines
// every affected Device, including browsers created while this dialog is open.
export function deviceRevocationMessage(
  device,
  devices = [],
  { signingOut = false } = {},
) {
  const parts = [
    signingOut
      ? t('settings.access.signOut.body')
      : t('settings.access.revoke.body', { name: device?.name ?? '' }),
    t('settings.access.revoke.cascade'),
  ];
  const affected = [];
  let issuers = device ? [device.id] : [];
  while (issuers.length > 0) {
    const next = devices.filter(
      (entry) =>
        entry.via === 'ticket' &&
        issuers.includes(entry.approved_by) &&
        entry.id !== device.id &&
        !affected.some((known) => known.id === entry.id),
    );
    affected.push(...next);
    issuers = next.map((entry) => entry.id);
  }
  if (affected.length > 0) {
    parts.push(
      t('settings.access.revoke.browsers', {
        names: affected.map((entry) => entry.name).join(', '),
      }),
    );
  }
  if (affected.some((entry) => entry.current)) {
    parts.push(t('settings.access.revoke.currentBrowser'));
  }
  return parts.join(' ');
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
