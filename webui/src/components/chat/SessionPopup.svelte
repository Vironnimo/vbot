<script>
  import { onMount } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import SessionView from '../SessionView.svelte';

  let { popup, onClose, onOpenFull } = $props();
  const navigation = useNavigation();
  let panel;
  let created = $state.raw(null);
  let error = $state('');
  let alive = false;
  const address = $derived(popup.address ?? created);
  const title = $derived(popup.title || t('navigation.chat'));

  async function load() {
    error = '';
    try {
      const address = await popup.resolve();
      if (alive) created = address;
    } catch (cause) {
      if (alive) error = cause.message || t('common.viewLoadError');
    }
  }

  onMount(() => {
    alive = true;
    const opener = document.activeElement;
    panel.focus({ preventScroll: true });
    const unregister = navigation?.registerLayer({ close: onClose });
    if (!popup.address) void load();
    return () => {
      alive = false;
      unregister?.();
      if (panel?.contains(document.activeElement) && opener?.isConnected) {
        opener.focus({ preventScroll: true });
      }
    };
  });

  function keydown(event) {
    if (
      event.key === 'Escape' &&
      !event.defaultPrevented &&
      !event.isComposing
    ) {
      event.preventDefault();
      onClose();
    }
  }
</script>

<!-- Nonmodal: the user can continue to navigate and edit the originating view. -->
<div
  class="session-popup"
  role="dialog"
  aria-label={title}
  tabindex="-1"
  bind:this={panel}
  onkeydown={keydown}
>
  <header>
    <h2>{title}</h2>
    {#if address}
      <Button variant="tertiary" onClick={() => onOpenFull(address)}>
        {t('appChat.openFull')}
      </Button>
    {/if}
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('common.close')}
      tooltip={t('common.close')}
      onClick={onClose}
    >
      <svg
        viewBox="0 0 16 16"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        aria-hidden="true"><path d="m4 4 8 8m0-8-8 8" /></svg
      >
    </Button>
  </header>
  {#if address}
    <div class="session-popup-body">
      <SessionView
        {address}
        hideOpenFull
        readOnly={popup.readOnly ?? false}
        getUIContext={popup.getUiContext}
        presentation={popup.presentation ?? 'conversation'}
      />
    </div>
  {:else if error}
    <Banner variant="error" role="alert"
      >{error}
      <Button variant="tertiary" onClick={load}>{t('common.retry')}</Button>
    </Banner>
  {:else}
    <p role="status">{t('common.loading')}</p>
  {/if}
</div>

<style>
  .session-popup {
    position: fixed;
    inset-inline-end: var(--space-md);
    inset-block-end: max(var(--space-md), env(safe-area-inset-bottom));
    width: min(32rem, calc(100% - 2 * var(--space-md)));
    height: min(36rem, 76dvh);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    box-shadow: var(--modal-elevation);
    background: var(--surface);
    overflow: hidden;
    z-index: var(--z-overlay);
  }
  header {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
    border-bottom: 1px solid var(--border);
  }
  h2 {
    margin: 0 auto 0 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-hi);
    font-size: var(--fs-body-md);
  }
  p {
    margin: var(--space-md);
    color: var(--text-med);
  }
  .session-popup-body {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
  }
</style>
