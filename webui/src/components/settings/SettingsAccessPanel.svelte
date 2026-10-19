<script>
  // Settings -> System -> Access & devices. The access mode is host
  // configuration: shown read-only with the command that changes it. The
  // paired Devices can be renamed and removed here, a new one approved by
  // its pairing code, and this browser signed out (the access gate then
  // shows the pairing screen). The list follows the server's `devices`
  // invalidation, so a Device approved elsewhere appears once it claimed its
  // approval.
  import { onDestroy } from 'svelte';
  import AddDeviceDialog from './AddDeviceDialog.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    ACCESS_LOSS_AUTH_REQUIRED,
    getAuthStatus,
    listDevices,
    logOutDevice,
    renameDevice,
    revokeDevice,
    revokeOtherDevices,
    signalAccessLoss,
  } from '$lib/api.js';
  import {
    ACCESS_MODE_AUTHENTICATED,
    accessModeDescription,
    describeDeviceUse,
    deviceErrorMessage,
    deviceRevocationMessage,
  } from '$lib/settingsView.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};
  const SHOW_COMMAND = 'vbot application access show';
  const DEVICE_NAME_MAX = 80;

  let { devicesRefreshToken = 0, onToast = noop } = $props();

  let status = $state(null);
  let statusError = $state('');
  let devices = $state([]);
  let devicesLoaded = $state(false);
  let devicesError = $state('');
  let statusRequest = 0;
  let devicesRequest = 0;
  let destroyed = false;

  onDestroy(() => {
    destroyed = true;
    statusRequest += 1;
    devicesRequest += 1;
  });

  let addOpen = $state(false);
  let renameTarget = $state(null);
  let renameValue = $state('');
  let revokeTarget = $state(null);
  let revokeOthersOpen = $state(false);
  let signOutOpen = $state(false);
  let dialogBusy = $state(false);
  let dialogError = $state('');

  const mode = $derived(status?.mode ?? '');
  const authenticatedMode = $derived(mode === ACCESS_MODE_AUTHENTICATED);
  // Only a paired Device can sign itself out; the host and the open modes
  // have no Device of their own.
  const signedInAsDevice = $derived(status?.principal === 'device');
  const otherDevices = $derived(devices.filter((device) => !device.current));

  // Reload on mount and whenever the server reports a change of the paired
  // Devices: one paired, renamed, revoked or expired, here or elsewhere.
  $effect(() => {
    void devicesRefreshToken;
    void loadStatus();
    void loadDevices();
  });

  async function loadStatus() {
    if (destroyed) return;
    const current = ++statusRequest;
    try {
      const result = await getAuthStatus();
      if (current !== statusRequest) return;
      status = result;
      statusError = '';
    } catch (error) {
      if (current !== statusRequest) return;
      statusError = `${t('settings.access.statusError')} ${error.message ?? ''}`;
    }
  }

  async function loadDevices() {
    if (destroyed) return;
    const current = ++devicesRequest;
    try {
      const result = await listDevices();
      if (current !== devicesRequest) return;
      devices = Array.isArray(result?.devices) ? result.devices : [];
      devicesError = '';
      devicesLoaded = true;
    } catch (error) {
      if (current !== devicesRequest) return;
      devicesError = deviceErrorMessage(error);
    }
  }

  function closeDialogs() {
    renameTarget = null;
    revokeTarget = null;
    revokeOthersOpen = false;
    signOutOpen = false;
    dialogBusy = false;
    dialogError = '';
  }

  function openRename(device) {
    closeDialogs();
    renameTarget = device;
    renameValue = device.name;
  }

  // Runs one dialog action; a failure stays in the open dialog.
  async function runDialogAction(action, onDone) {
    if (dialogBusy) {
      return;
    }
    dialogBusy = true;
    dialogError = '';
    try {
      const result = await action();
      closeDialogs();
      onDone?.(result);
    } catch (error) {
      dialogError = deviceErrorMessage(error);
      dialogBusy = false;
      if (error?.code === 'device_not_found') void loadDevices();
    }
  }

  function submitRename(event) {
    event.preventDefault();
    const name = renameValue.trim();
    if (!renameTarget || !name) {
      return;
    }
    const id = renameTarget.id;
    void runDialogAction(
      () => renameDevice(id, name),
      () => {
        onToast({
          title: t('settings.access.rename.done'),
          variant: 'success',
        });
        void loadDevices();
      },
    );
  }

  function confirmRevoke() {
    const device = revokeTarget;
    if (!device) {
      return;
    }
    void runDialogAction(
      () => revokeDevice(device.id),
      () => {
        onToast({
          title: t('settings.access.revoke.done', { name: device.name }),
          variant: 'success',
        });
        void loadDevices();
      },
    );
  }

  function confirmRevokeOthers() {
    void runDialogAction(
      () => revokeOtherDevices(),
      (result) => {
        onToast({
          title: t('settings.access.revokeOthers.done', {
            count: result?.revoked?.length ?? 0,
          }),
          variant: 'success',
        });
        void loadDevices();
      },
    );
  }

  // Signing out takes this page's access away; the access gate takes over.
  function confirmSignOut() {
    void runDialogAction(
      () => logOutDevice(),
      () => signalAccessLoss(ACCESS_LOSS_AUTH_REQUIRED),
    );
  }

  function handleDecided({ approved, name }) {
    addOpen = false;
    onToast({
      title: approved
        ? t('settings.access.add.approved', { name })
        : t('settings.access.add.denied', { name }),
      variant: approved ? 'success' : 'info',
    });
    // The approved Device appears with the `devices` invalidation its claim
    // publishes; until then the approval toast stands for it.
  }
</script>

{#if statusError}
  <Banner variant="error" role="alert">
    <span>{statusError}</span>
    <Button variant="secondary" onClick={loadStatus}>
      {t('common.retry')}
    </Button>
  </Banner>
{/if}

<div class="s-group">
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">
        {t('settings.access.mode')}
        <InfoHint text={t('settings.access.modeHelp')} />
      </div>
      {#if mode}
        <div class="s-row-desc">{accessModeDescription(mode)}</div>
      {/if}
    </div>
    <div class="s-row-control access-fact">
      <span class="access-fact__value" data-access-mode={mode || null}>
        {status?.mode_label ?? t('common.loading')}
      </span>
    </div>
  </div>
  <div class="s-row">
    <div class="s-row-info">
      <div class="s-row-label">{t('settings.access.change')}</div>
      <div class="s-row-desc">{t('settings.access.changeDescription')}</div>
    </div>
    <div class="s-row-control access-fact">
      <code class="access-fact__command">{SHOW_COMMAND}</code>
      <CopyButton
        text={SHOW_COMMAND}
        label={t('settings.access.copyCommand')}
      />
    </div>
  </div>
</div>

<div class="s-subhead">
  <h4 class="s-subhead__title">{t('settings.access.devices.title')}</h4>
  {#if mode}
    <p class="s-subhead__desc">
      {authenticatedMode
        ? t('settings.access.devices.description')
        : t('settings.access.devices.descriptionOpen')}
    </p>
  {/if}
</div>

{#if authenticatedMode}
  <div class="access-actions">
    <Button variant="secondary" onClick={() => (addOpen = true)}>
      {t('settings.access.add.button')}
    </Button>
    <Button
      variant="secondary"
      disabled={otherDevices.length === 0}
      onClick={() => {
        closeDialogs();
        revokeOthersOpen = true;
      }}
    >
      {t('settings.access.revokeOthers.button')}
    </Button>
  </div>
{/if}

{#if devicesError}
  <Banner variant="error" role="alert">
    <span>{devicesError}</span>
    <Button variant="secondary" onClick={loadDevices}>
      {t('common.retry')}
    </Button>
  </Banner>
{:else if !devicesLoaded}
  <Banner variant="neutral">{t('common.loading')}</Banner>
{:else if devices.length === 0}
  <EmptyState
    density="compact"
    description={authenticatedMode
      ? t('settings.access.devices.empty')
      : t('settings.access.devices.emptyOpen')}
  />
{:else}
  <div
    class="s-group"
    role="list"
    aria-label={t('settings.access.devices.title')}
  >
    {#each devices as device (device.id)}
      <div class="s-row s-row--compact access-device" role="listitem">
        <div class="s-row-info">
          <div class="access-device__head">
            <span class="s-row-label">{device.name}</span>
            {#if device.current}
              <StatusChip variant="info">
                {t('settings.access.devices.thisDevice')}
              </StatusChip>
            {/if}
          </div>
          <div class="s-row-desc">{describeDeviceUse(device, devices)}</div>
        </div>
        <div class="s-row-control access-device__actions">
          <Button
            variant="tertiary"
            ariaLabel={t('settings.access.rename.aria', { name: device.name })}
            onClick={() => openRename(device)}
          >
            {t('settings.access.rename.button')}
          </Button>
          {#if device.current && signedInAsDevice}
            <Button
              variant="secondary"
              onClick={() => {
                closeDialogs();
                signOutOpen = true;
              }}
            >
              {t('settings.access.signOut.button')}
            </Button>
          {:else}
            <Button
              variant="secondary"
              ariaLabel={t('settings.access.revoke.aria', {
                name: device.name,
              })}
              onClick={() => {
                closeDialogs();
                revokeTarget = device;
              }}
            >
              {t('settings.access.revoke.button')}
            </Button>
          {/if}
        </div>
      </div>
    {/each}
  </div>
{/if}

{#if addOpen}
  <AddDeviceDialog
    onClose={() => (addOpen = false)}
    onDecided={handleDecided}
  />
{/if}

{#if renameTarget}
  <Modal
    title={t('settings.access.rename.title')}
    labelledById="rename-device-title"
    closeDisabled={dialogBusy}
    onClose={closeDialogs}
  >
    {#snippet body()}
      <form id="rename-device-form" onsubmit={submitRename}>
        <div class="modal-body">
          <FormField
            controlId="rename-device-name"
            label={t('settings.access.rename.label')}
            error={dialogError}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                maxlength={DEVICE_NAME_MAX}
                aria-describedby={field.describedBy}
                invalid={field.invalid}
                value={renameValue}
                disabled={dialogBusy}
                onInput={(next) => {
                  renameValue = next;
                  dialogError = '';
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" disabled={dialogBusy} onClick={closeDialogs}>
        {t('common.cancel')}
      </Button>
      <Button
        type="submit"
        form="rename-device-form"
        variant="primary"
        disabled={!renameValue.trim()}
        loading={dialogBusy}
      >
        {t('common.save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if revokeTarget}
  <ConfirmDialog
    title={t('settings.access.revoke.title', { name: revokeTarget.name })}
    body={deviceRevocationMessage(revokeTarget, devices)}
    confirmLabel={t('settings.access.revoke.confirm')}
    busy={dialogBusy}
    error={dialogError}
    onConfirm={confirmRevoke}
    onCancel={closeDialogs}
  />
{/if}

{#if revokeOthersOpen}
  <ConfirmDialog
    title={t('settings.access.revokeOthers.title')}
    body={signedInAsDevice
      ? t('settings.access.revokeOthers.body')
      : t('settings.access.revokeOthers.bodyAll')}
    confirmLabel={t('settings.access.revokeOthers.confirm')}
    busy={dialogBusy}
    error={dialogError}
    onConfirm={confirmRevokeOthers}
    onCancel={closeDialogs}
  />
{/if}

{#if signOutOpen}
  <ConfirmDialog
    title={t('settings.access.signOut.title')}
    body={deviceRevocationMessage(
      devices.find((device) => device.current),
      devices,
      { signingOut: true },
    )}
    confirmLabel={t('settings.access.signOut.confirm')}
    busy={dialogBusy}
    error={dialogError}
    onConfirm={confirmSignOut}
    onCancel={closeDialogs}
  />
{/if}

<style>
  /* Read-only facts read as plain values, like the server facts above. */
  .s-row > .s-row-control.access-fact {
    justify-content: flex-start;
    gap: 8px;
  }

  .access-fact__value {
    flex: 1 1 auto;
    min-width: 0;
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }

  .access-fact__command {
    flex: 1 1 auto;
    min-width: 0;
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    line-height: 1.5;
    overflow-wrap: anywhere;
  }

  .access-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 8px 0;
  }

  .access-device__head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .access-device__actions {
    gap: 6px;
  }
</style>
