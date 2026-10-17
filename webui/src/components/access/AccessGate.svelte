<script>
  // The boot gate, mounted in place of the app (main.js passes `App`). It asks
  // the server before the app mounts whether this page may use it, shows the
  // pairing screen or a refusal instead, and takes the app down again when a
  // transport reports an access loss (lib/accessGate.js owns the decisions).
  import { onDestroy } from 'svelte';

  import Button from '../ui/Button.svelte';
  import PairingScreen from './PairingScreen.svelte';
  import { getAuthStatus, onAccessLoss } from '$lib/api.js';
  import {
    GATE_APP,
    GATE_CHECKING,
    GATE_DENIED,
    GATE_LOOPING,
    GATE_PAIRING,
    GATE_UNREACHABLE,
    createAccessGate,
  } from '$lib/accessGate.js';
  import { flushComposerMemory } from '$lib/composerMemory.js';
  import { t } from '$lib/i18n.js';

  let { App } = $props();

  let gateState = $state({ screen: GATE_CHECKING, status: null, notice: null });

  const gate = createAccessGate({
    getAuthStatus,
    onAccessLoss,
    // Unsent Chat composer text survives until the app mounts again.
    beforeLeaveApp: flushComposerMemory,
    onChange: (next) => {
      gateState = next;
    },
  });
  void gate.start();
  onDestroy(() => gate.destroy());

  // A gate screen replaces whatever had focus; its heading takes it, so
  // keyboard and screen reader users start at the new content.
  function focusOnMount(node) {
    node.focus();
  }
</script>

{#if gateState.screen === GATE_APP}
  <App />
{:else if gateState.screen === GATE_CHECKING}
  <div class="access-gate access-gate--checking" aria-busy="true">
    <p class="access-gate__checking" role="status">
      {t('access.gate.checking')}
    </p>
  </div>
{:else}
  <main class="access-gate">
    <section class="access-gate__card" aria-labelledby="access-gate-title">
      <img
        class="access-gate__mark"
        src="/brand/vbot-mark-transparent.png"
        alt=""
        width="48"
        height="48"
      />
      {#if gateState.screen === GATE_PAIRING}
        <PairingScreen
          notice={gateState.notice}
          onApproved={() => gate.pairingApproved()}
          onRecheck={() => gate.recheck()}
        />
      {:else if gateState.screen === GATE_DENIED}
        <h1
          id="access-gate-title"
          class="access-gate__title"
          tabindex="-1"
          use:focusOnMount
        >
          {t('access.denied.title')}
        </h1>
        <p class="access-gate__text">{t('access.denied.body')}</p>
        <p class="access-gate__text">{t('access.denied.hint')}</p>
        <div class="access-gate__actions">
          <Button variant="primary" onClick={() => gate.retry()}>
            {t('common.retry')}
          </Button>
        </div>
      {:else if gateState.screen === GATE_UNREACHABLE}
        <h1
          id="access-gate-title"
          class="access-gate__title"
          tabindex="-1"
          use:focusOnMount
        >
          {t('access.unreachable.title')}
        </h1>
        <p class="access-gate__text" role="status">
          {t('access.unreachable.body')}
        </p>
        <div class="access-gate__actions">
          <Button variant="primary" onClick={() => gate.retry()}>
            {t('common.retry')}
          </Button>
        </div>
      {:else if gateState.screen === GATE_LOOPING}
        <h1
          id="access-gate-title"
          class="access-gate__title"
          tabindex="-1"
          use:focusOnMount
        >
          {t('access.looping.title')}
        </h1>
        <p class="access-gate__text">{t('access.looping.body')}</p>
        <div class="access-gate__actions">
          <Button variant="primary" onClick={() => gate.retry()}>
            {t('common.retry')}
          </Button>
        </div>
      {/if}
    </section>
  </main>
{/if}

<style>
  .access-gate {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    min-height: 100dvh;
    padding: 24px 16px;
    background: var(--bg);
    color: var(--text-body);
    font-family: var(--font-ui);
  }

  .access-gate__card {
    display: grid;
    gap: 14px;
    width: min(100%, 520px);
    padding: 28px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--surface);
  }

  .access-gate__mark {
    width: 48px;
    height: 48px;
  }

  /* The checking note appears only when the server is slow to answer, so a
     fast check does not flash it. */
  .access-gate__checking {
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    opacity: 0;
    animation: access-gate-reveal 0.2s ease-out 0.6s forwards;
  }

  @keyframes access-gate-reveal {
    to {
      opacity: 1;
    }
  }

  .access-gate__title,
  .access-gate :global(.access-gate__title) {
    margin: 0;
    color: var(--text-hi);
    font: 600 var(--fs-heading-lg) / 1.3 var(--font-ui);
  }

  .access-gate__title:focus,
  .access-gate :global(.access-gate__title:focus) {
    outline: none;
  }

  .access-gate__text,
  .access-gate :global(.access-gate__text) {
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-md);
    line-height: 1.55;
  }

  .access-gate__actions,
  .access-gate :global(.access-gate__actions) {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  @media (max-width: 640px) {
    .access-gate__card {
      padding: 20px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .access-gate__checking {
      animation-duration: 0s;
    }
  }
</style>
