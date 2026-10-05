<script>
  // The microphone Wakeword listening and Desktop dictation record from, and
  // whether echo cancellation cleans its signal. Each change applies at once:
  // the Desktop saves it and answers with the resulting settings, which stay
  // authoritative here. What the running capture does with them (the
  // microphone in use, the echo cancellation state) shows under Wakeword.
  import { onMount } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { bridgeErrorMessage } from '../voice/voiceLabels.js';
  import {
    getDesktopMicrophone,
    listMicrophones,
    setDesktopMicrophone,
  } from '$lib/desktopBridge.js';
  import { t } from '$lib/i18n.js';

  // The value of the chosen device while it is not connected.
  const NOT_CONNECTED_VALUE = '__not_connected__';
  const noop = () => {};

  let {
    // The app-level Desktop Voice owner (`status`), or null; a running
    // calibration would restart on a microphone change.
    desktopVoice = null,
    onToast = noop,
  } = $props();

  let settings = $state(null);
  let devices = $state([]);
  let loadError = $state(false);
  let busy = $state(false);
  let destroyed = false;
  let listing = null;

  let calibrating = $derived(Boolean(desktopVoice?.status?.calibration));
  let controlsDisabled = $derived(!settings || busy || calibrating);
  let chosenDevice = $derived(connectedDevice(devices, settings?.device));
  let notConnected = $derived(Boolean(settings?.device) && !chosenDevice);
  let deviceOptions = $derived([
    { value: '', label: t('settings.microphone.systemDefault') },
    ...(notConnected
      ? [
          {
            value: NOT_CONNECTED_VALUE,
            label: settings.device.name,
            secondaryLabel: t('settings.microphone.notConnected'),
            disabled: true,
          },
        ]
      : []),
    ...devices.map((device) => ({
      value: String(device.index),
      label: device.name,
      secondaryLabel: device.supported
        ? t('settings.microphone.compatible')
        : t('settings.microphone.unsupported'),
      disabled: !device.supported,
    })),
  ]);
  let selectedValue = $derived(
    chosenDevice
      ? String(chosenDevice.index)
      : notConnected
        ? NOT_CONNECTED_VALUE
        : '',
  );

  // The listed device the Desktop records from for a stored choice: the one
  // with the same name and host API, preferring the stored index when several
  // match. Null when none (or several without the stored index) match.
  function connectedDevice(listed, stored) {
    if (!stored) return null;
    const matches = listed.filter(
      (device) =>
        device.name === stored.name &&
        (device.host_api || '') === stored.host_api,
    );
    return (
      matches.find((device) => device.index === stored.index) ??
      (matches.length === 1 ? matches[0] : null)
    );
  }

  async function load() {
    loadError = false;
    try {
      const [current, listed] = await Promise.all([
        getDesktopMicrophone(),
        listMicrophones(),
      ]);
      if (destroyed) return;
      devices = listed;
      settings = current;
    } catch {
      if (!destroyed) loadError = true;
    }
  }

  // Devices come and go; the list is read again whenever it opens.
  function refreshDevices() {
    listing ??= listMicrophones()
      .then((listed) => {
        if (!destroyed) devices = listed;
      })
      .catch(() => {})
      .finally(() => {
        listing = null;
      });
  }

  async function update(changes) {
    busy = true;
    try {
      const next = await setDesktopMicrophone(changes);
      if (!destroyed) settings = next;
    } catch (error) {
      if (!destroyed)
        onToast({
          title: t('errors.generic'),
          message: bridgeErrorMessage(error),
          variant: 'error',
        });
    } finally {
      busy = false;
    }
  }

  function handleDeviceChange(value) {
    const index = Number.parseInt(value, 10);
    const device = Number.isInteger(index)
      ? devices.find((candidate) => candidate.index === index)
      : null;
    if (value && !device) return;
    void update({
      device: device
        ? {
            index: device.index,
            name: device.name,
            host_api: device.host_api || '',
          }
        : null,
    });
  }

  onMount(() => {
    void load();
    return () => {
      destroyed = true;
    };
  });
</script>

<div class="s-group">
  {#if loadError}
    <div class="s-group__block">
      <Banner variant="error" role="alert">
        <span>{t('settings.microphone.loadError')}</span>
        <Button variant="secondary" onClick={load}>
          {t('common.retry')}
        </Button>
      </Banner>
    </div>
  {:else}
    <div class="s-row">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.microphone.device')}
          <InfoHint
            text={t('settings.microphone.deviceHelp')}
            ariaLabel={t('settings.microphone.deviceHelpAria')}
          />
        </div>
        <div class="s-row-desc">
          {t('settings.microphone.deviceDescription')}
        </div>
      </div>
      <div class="s-row-control">
        <Dropdown
          value={selectedValue}
          options={deviceOptions}
          ariaLabel={t('settings.microphone.device')}
          disabled={controlsDisabled}
          onValueChange={handleDeviceChange}
          onOpenChange={(open) => {
            if (open) refreshDevices();
          }}
        />
      </div>
    </div>

    {#if notConnected}
      <div class="s-group__block s-group__block--attached">
        <Banner variant="warn" role="status">
          {t('settings.microphone.notConnectedWarning')}
        </Banner>
      </div>
    {/if}

    <div class="s-row s-row--compact">
      <div class="s-row-info">
        <div class="s-row-label">
          {t('settings.microphone.echoCancellation')}
          <InfoHint
            text={t('settings.microphone.echoCancellationHelp')}
            ariaLabel={t('settings.microphone.echoCancellationHelpAria')}
          />
        </div>
      </div>
      <div class="s-row-control">
        <Toggle
          checked={settings?.echo_cancellation ?? true}
          onChange={(checked) => update({ echo_cancellation: checked })}
          disabled={controlsDisabled}
          ariaLabel={t('settings.microphone.echoCancellationAria')}
        />
      </div>
    </div>
  {/if}
</div>
