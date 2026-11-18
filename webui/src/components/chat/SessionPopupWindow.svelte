<script>
  import { onMount, tick } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import Button from '../ui/Button.svelte';

  let { title, active, opener, onClose, onOpenFull, children } = $props();
  const navigation = useNavigation();
  const moveHintId = $props.id();
  let panel;
  let titleBar;
  let restoreButton = $state();
  let lastContentFocus;
  let minimized = $state(false);
  let position = $state.raw(null);
  let dragging = $state.raw(null);
  const visible = $derived(active && !minimized);

  $effect(() => {
    if (visible) return navigation?.registerLayer({ close: onClose });
  });

  onMount(() => {
    panel.focus({ preventScroll: true });
    return () => {
      const focused = document.activeElement;
      if (
        panel?.contains(focused) ||
        focused === restoreButton ||
        focused === document.body
      ) {
        void tick().then(() => {
          if (
            document.activeElement === document.body &&
            opener?.isConnected &&
            !opener.closest('[hidden], [inert]')
          )
            opener.focus({ preventScroll: true });
        });
      }
    };
  });

  function rememberFocus(event) {
    if (
      event.target !== panel &&
      !event.target.closest('.session-popup > header')
    )
      lastContentFocus = event.target;
  }

  async function toggleMinimized() {
    dragging = null;
    minimized = !minimized;
    await tick();
    if (!active || !panel?.isConnected) return;
    if (minimized) restoreButton?.focus({ preventScroll: true });
    else {
      const target =
        lastContentFocus?.isConnected && panel.contains(lastContentFocus)
          ? lastContentFocus
          : panel;
      target.focus({ preventScroll: true });
    }
  }

  function startDrag(event) {
    if (
      !event.isPrimary ||
      event.button !== 0 ||
      !titleBar.contains(event.target) ||
      event.target.closest('button')
    )
      return;
    const rect = panel.getBoundingClientRect();
    dragging = {
      pointer: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function drag(event) {
    if (dragging?.pointer === event.pointerId)
      position = {
        x: event.clientX - dragging.offsetX,
        y: event.clientY - dragging.offsetY,
      };
  }

  function endDrag(event) {
    if (dragging?.pointer === event.pointerId) dragging = null;
  }

  function keydown(event) {
    if (event.defaultPrevented || event.isComposing) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
    } else if (
      event.target === panel &&
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey
    ) {
      const step = event.shiftKey ? 48 : 12;
      const offset = {
        ArrowUp: [0, -step],
        ArrowDown: [0, step],
        ArrowLeft: [-step, 0],
        ArrowRight: [step, 0],
      }[event.key];
      if (!offset) return;
      event.preventDefault();
      const rect = panel.getBoundingClientRect();
      position = { x: rect.left + offset[0], y: rect.top + offset[1] };
    }
  }
</script>

<!-- Nonmodal: the user can continue to navigate and edit the originating view. -->
<div
  class="session-popup"
  role="dialog"
  aria-label={title}
  aria-describedby={moveHintId}
  tabindex="0"
  class:positioned={position !== null}
  style:--popup-x={position && `${position.x}px`}
  style:--popup-y={position && `${position.y}px`}
  bind:this={panel}
  hidden={!visible}
  inert={!visible}
  onfocusin={rememberFocus}
  onkeydown={keydown}
  onpointerdown={startDrag}
  onpointermove={drag}
  onpointerup={endDrag}
  onpointercancel={endDrag}
  onlostpointercapture={endDrag}
>
  <span hidden id={moveHintId}>{t('appChat.moveHint')}</span>
  <header bind:this={titleBar} class:dragging={dragging !== null}>
    <h2>{title}</h2>
    {#if onOpenFull}
      <Button
        variant="tertiary"
        ariaLabel={t('appChat.openFull')}
        tooltip={t('appChat.openFull')}
        onClick={onOpenFull}
      >
        <span class="expand-label">{t('appChat.openFull')}</span>
        <svg
          class="expand-icon"
          viewBox="0 0 16 16"
          width="14"
          height="14"
          fill="none"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path d="M8 3h5v5m0-5L7 9M6 4H3v9h9v-3" />
        </svg>
      </Button>
    {/if}
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('appChat.minimize')}
      tooltip={t('appChat.minimize')}
      onClick={toggleMinimized}
    >
      <svg
        viewBox="0 0 16 16"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        aria-hidden="true"><path d="M3 11.5h10" /></svg
      >
    </Button>
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
  {@render children(visible)}
</div>

{#if active && minimized}
  <button
    class="session-popup-task"
    type="button"
    aria-label={t('appChat.restore', { title })}
    bind:this={restoreButton}
    onclick={toggleMinimized}
  >
    <svg
      viewBox="0 0 20 20"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"><path d="M16.5 3.5h-13v10h3v3l4-3h6z" /></svg
    >
    <span>{title}</span>
    <svg
      viewBox="0 0 16 16"
      width="13"
      height="13"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"><path d="m3.5 10 4.5-4.5 4.5 4.5" /></svg
    >
  </button>
{/if}

<style>
  .session-popup[hidden] {
    display: none;
  }
  .session-popup,
  .session-popup-task {
    --gap-left: max(var(--space-md), env(safe-area-inset-left));
    --gap-right: max(var(--space-md), env(safe-area-inset-right));
    --gap-top: max(var(--space-md), env(safe-area-inset-top));
    --gap-bottom: max(var(--space-md), env(safe-area-inset-bottom));
    --popup-width: min(26rem, calc(100% - var(--gap-left) - var(--gap-right)));
    --popup-height: min(
      28rem,
      70dvh,
      calc(100dvh - var(--gap-top) - var(--gap-bottom))
    );
    position: fixed;
    right: var(--gap-right);
    bottom: var(--gap-bottom);
    z-index: var(--z-overlay);
  }
  .session-popup {
    container: chat-popup / inline-size;
    width: var(--popup-width);
    height: var(--popup-height);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    box-shadow: var(--modal-elevation);
    background: var(--surface);
    overflow: hidden;
  }
  .session-popup.positioned {
    right: auto;
    bottom: auto;
    left: clamp(
      var(--gap-left),
      var(--popup-x),
      calc(100% - var(--popup-width) - var(--gap-right))
    );
    top: clamp(
      var(--gap-top),
      var(--popup-y),
      calc(100dvh - var(--popup-height) - var(--gap-bottom))
    );
  }
  .session-popup-task {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    max-width: var(--popup-width);
    min-height: 2.5rem;
    padding: var(--space-sm) var(--space-md);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    box-shadow: var(--modal-elevation);
    background: var(--surface);
    color: var(--text-hi);
    font: inherit;
    cursor: pointer;
  }
  .session-popup-task span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .session-popup-task svg {
    flex: none;
  }
  header {
    flex: none;
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    padding: var(--space-sm) var(--space-md);
    border-bottom: 1px solid var(--border);
    cursor: grab;
    touch-action: none;
    user-select: none;
  }
  .expand-icon {
    display: none;
  }
  @container chat-popup (max-width: 23rem) {
    .expand-label {
      display: none;
    }
    .expand-icon {
      display: block;
    }
  }
  header.dragging {
    cursor: grabbing;
  }
  h2 {
    margin: 0 auto 0 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-hi);
    font-size: var(--fs-body-md);
  }
</style>
