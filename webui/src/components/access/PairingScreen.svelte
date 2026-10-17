<script>
  // Pairing screen of the access gate: this browser asks the server for a
  // pairing code, shows it with where to enter it, and waits until a
  // signed-in device or the host approves it (lib/pairing.js owns the
  // request, the polling and the expiry). Approval sets the device cookie;
  // the gate then checks the server again and mounts the app.
  import { onDestroy, onMount } from 'svelte';

  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import { DEVICE_KIND_BROWSER, pollPairing, startPairing } from '$lib/api.js';
  import { GATE_NOTICE_NOT_KEPT } from '$lib/accessGate.js';
  import {
    PAIRING_APPROVED,
    PAIRING_DENIED,
    PAIRING_EXPIRED,
    PAIRING_FAILED,
    PAIRING_RATE_LIMITED,
    PAIRING_STARTING,
    PAIRING_WAITING,
    createPairing,
    describeBrowserDevice,
  } from '$lib/pairing.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';

  const noop = () => {};

  let { notice = null, onApproved = noop, onRecheck = noop } = $props();

  const deviceName = browserDeviceName();
  let pairingState = $state({ phase: null });

  const pairing = createPairing({
    startPairing,
    pollPairing,
    deviceName,
    kind: DEVICE_KIND_BROWSER,
    onChange: (next) => {
      pairingState = next;
    },
    onApproved: (device) => onApproved(device),
    onRecheck: () => onRecheck(),
  });

  // An approved browser that did not keep its sign-in waits for the user:
  // a new code would fail the same way until the address changes.
  onMount(() => {
    if (notice !== GATE_NOTICE_NOT_KEPT) void pairing.start();
  });
  onDestroy(() => pairing.destroy());

  const phase = $derived(pairingState.phase);
  const code = $derived(
    phase === PAIRING_WAITING ? String(pairingState.code ?? '') : '',
  );
  const approveCommand = $derived(code ? `vbot devices approve ${code}` : '');
  // The code read character by character, so a screen reader does not try
  // to pronounce it as a word.
  const spelledCode = $derived(
    code
      .split('-')
      .map((part) => part.split('').join(' '))
      .join(', '),
  );
  const expiresAtLabel = $derived(
    phase === PAIRING_WAITING && Number.isFinite(pairingState.expiresAt)
      ? new Date(pairingState.expiresAt).toLocaleTimeString(activeLocaleTag(), {
          hour: '2-digit',
          minute: '2-digit',
        })
      : '',
  );
  const showRestart = $derived(
    notice === GATE_NOTICE_NOT_KEPT && phase === null
      ? true
      : [
          PAIRING_DENIED,
          PAIRING_EXPIRED,
          PAIRING_FAILED,
          PAIRING_RATE_LIMITED,
        ].includes(phase),
  );
  const restartLabel = $derived(
    phase === PAIRING_EXPIRED
      ? t('access.pairing.newCode')
      : phase === PAIRING_RATE_LIMITED
        ? t('access.pairing.tryAgain')
        : t('access.pairing.startAgain'),
  );

  function browserDeviceName() {
    const { browser, os } = describeBrowserDevice();
    if (browser && os) {
      return t('access.pairing.deviceNameBrowserOs', { browser, os });
    }
    if (browser) {
      return browser;
    }
    if (os) {
      return t('access.pairing.deviceNameOs', { os });
    }
    return t('access.pairing.deviceNameFallback');
  }

  function failureMessage(error) {
    if (error === 'devices_unavailable') {
      return t('access.pairing.error.devicesUnavailable');
    }
    if (error === 'network_error') {
      return t('access.pairing.error.network');
    }
    return t('access.pairing.error.generic');
  }

  function focusOnMount(node) {
    node.focus();
  }

  function focusButton(node) {
    node.querySelector('button:not([disabled])')?.focus();
  }
</script>

<h1
  id="access-gate-title"
  class="access-gate__title"
  tabindex="-1"
  use:focusOnMount
>
  {t('access.pairing.title')}
</h1>
<p class="access-gate__text">{t('access.pairing.intro')}</p>

{#if notice === GATE_NOTICE_NOT_KEPT && phase === null}
  <Banner variant="warn" role="alert">{t('access.pairing.notKept')}</Banner>
{/if}

<!-- One polite live region carries the code and the wait state, so a screen
     reader hears the code once it arrives and every change of state. -->
<div class="pairing__status" role="status" aria-live="polite">
  {#if phase === PAIRING_STARTING}
    <p class="access-gate__text">{t('access.pairing.starting')}</p>
  {:else if phase === PAIRING_WAITING}
    <p class="pairing__code-label">{t('access.pairing.codeLabel')}</p>
    <p class="pairing__code">
      <span aria-hidden="true">{code}</span>
      <span class="pairing__spoken">{spelledCode}</span>
    </p>
    <p class="pairing__wait">
      {#if pairingState.unstable}
        {t('access.pairing.unstable')}
      {:else}
        {t('access.pairing.waiting', { time: expiresAtLabel })}
      {/if}
    </p>
  {:else if phase === PAIRING_APPROVED}
    <p class="access-gate__text">{t('access.pairing.approved')}</p>
  {:else if phase === PAIRING_DENIED}
    <Banner variant="error">{t('access.pairing.denied')}</Banner>
  {:else if phase === PAIRING_EXPIRED}
    <Banner variant="warn">{t('access.pairing.expired')}</Banner>
  {:else if phase === PAIRING_RATE_LIMITED}
    <Banner variant="warn">{t('access.pairing.rateLimited')}</Banner>
  {:else if phase === PAIRING_FAILED}
    <Banner variant="error">{failureMessage(pairingState.error)}</Banner>
  {/if}
</div>

{#if phase === PAIRING_WAITING}
  <ol class="pairing__steps">
    <li>{t('access.pairing.stepDevice')}</li>
    <li>
      {t('access.pairing.stepServer')}
      <span class="pairing__command">
        <code>{approveCommand}</code>
        <CopyButton
          text={approveCommand}
          label={t('access.pairing.copyCommand')}
        />
      </span>
    </li>
  </ol>
  <p class="pairing__device">
    {t('access.pairing.deviceName', { name: deviceName })}
  </p>
{/if}

{#if showRestart}
  <!-- The way on takes focus when the request ended. -->
  {#key phase}
    <div class="access-gate__actions" use:focusButton>
      <Button
        variant="primary"
        disabled={phase === PAIRING_RATE_LIMITED && !pairingState.canRetry}
        onClick={() => pairing.start()}
      >
        {restartLabel}
      </Button>
    </div>
  {/key}
{/if}

<style>
  .pairing__status {
    display: grid;
    gap: 6px;
  }

  .pairing__code-label {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }

  .pairing__code {
    color: var(--text-hi);
    font: 600 32px / 1.2 var(--font-mono);
    letter-spacing: 0.12em;
  }

  /* Read by screen readers only; the visible code is hidden from them. */
  .pairing__spoken {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  .pairing__wait,
  .pairing__device {
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }

  .pairing__steps {
    display: grid;
    gap: 10px;
    margin: 0;
    padding-left: 20px;
    color: var(--text-body);
    font-size: var(--fs-body-md);
    line-height: 1.55;
  }

  .pairing__command {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 4px;
  }

  .pairing__command code {
    min-width: 0;
    padding: 4px 8px;
    border-radius: 6px;
    background: var(--surface-2);
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    overflow-wrap: anywhere;
  }
</style>
