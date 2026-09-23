<script module>
  let infoHintSequence = 0;

  function nextPopoverId() {
    infoHintSequence += 1;
    return `info-hint-popover-${infoHintSequence}`;
  }
</script>

<script>
  // The "?" info dot - the explanatory tier of the shared floating layers
  // (quick label tooltips and hover cards are in lib/tooltip.js). Hover
  // previews the popover after the shared hover intent, keyboard focus
  // previews it at once, and click/tap pins it until a second click, an
  // outside press, or Escape. Scrolling repositions it. Callers pass
  // already-translated `text`; blank lines separate paragraphs.
  import { onDestroy } from 'svelte';

  import { portal } from '../../lib/dropdownPanel.js';
  import { t } from '../../lib/i18n.js';
  import {
    FLOATING_HOVER_CLOSE_DELAY_MS,
    HOVER_CARD_SHOW_DELAY_MS,
    createFloatingLayer,
    isKeyboardModality,
    trackInputModality,
  } from '../../lib/tooltip.js';

  let { text = '', ariaLabel = '', class: className = '' } = $props();

  const popoverId = nextPopoverId();
  const releaseModality = trackInputModality();

  let open = $state(false);
  let pinned = $state(false);
  let dotElement = $state(null);
  let popoverElement = $state(null);
  let showTimer = null;
  let closeTimer = null;

  // A pinned popover absorbs Escape so an enclosing dialog stays open.
  const layer = createFloatingLayer({
    kind: 'popover',
    anchor: () => dotElement,
    element: () => popoverElement,
    onDismiss: close,
    onEscape: (event) => {
      if (pinned) {
        event.preventDefault();
      }
      close();
    },
  });

  let label = $derived(ariaLabel || t('common.moreInfo', 'More information'));
  let paragraphs = $derived(
    String(text)
      .split(/\n{2,}/)
      .map((paragraph) => paragraph.trim())
      .filter(Boolean),
  );
  let visible = $derived(open && paragraphs.length > 0);

  function cancelShow() {
    if (showTimer !== null) {
      clearTimeout(showTimer);
      showTimer = null;
    }
  }

  function cancelClose() {
    if (closeTimer !== null) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
  }

  function close() {
    cancelShow();
    cancelClose();
    open = false;
    pinned = false;
  }

  function scheduleClose() {
    cancelShow();
    if (pinned || !open) {
      return;
    }
    cancelClose();
    closeTimer = setTimeout(() => {
      closeTimer = null;
      if (!pinned) {
        open = false;
      }
    }, FLOATING_HOVER_CLOSE_DELAY_MS);
  }

  function onDotPointerEnter(event) {
    if (event.pointerType === 'touch') {
      return;
    }
    cancelClose();
    if (open) {
      return;
    }
    cancelShow();
    showTimer = setTimeout(() => {
      showTimer = null;
      open = true;
    }, HOVER_CARD_SHOW_DELAY_MS);
  }

  function onPointerLeave(event) {
    if (event.pointerType !== 'touch') {
      scheduleClose();
    }
  }

  function onDotFocus() {
    if (isKeyboardModality()) {
      cancelShow();
      cancelClose();
      open = true;
    }
  }

  function onDotClick() {
    if (pinned) {
      close();
      return;
    }
    cancelShow();
    cancelClose();
    pinned = true;
    open = true;
  }

  $effect(() => {
    if (!visible || !popoverElement || !dotElement) {
      return undefined;
    }
    layer.show({ pinned });
    return () => layer.hide();
  });

  onDestroy(() => {
    cancelShow();
    cancelClose();
    layer.hide();
    releaseModality();
  });
</script>

<button
  bind:this={dotElement}
  type="button"
  class={['info-hint', className].filter(Boolean).join(' ')}
  aria-label={label}
  aria-expanded={open}
  aria-describedby={visible ? popoverId : undefined}
  onpointerenter={onDotPointerEnter}
  onpointerleave={onPointerLeave}
  onfocus={onDotFocus}
  onblur={scheduleClose}
  onclick={onDotClick}>?</button
>

{#if visible}
  <div
    bind:this={popoverElement}
    use:portal
    class="floating-card info-popover"
    id={popoverId}
    role="tooltip"
    data-floating-open="true"
    onpointerenter={cancelClose}
    onpointerleave={onPointerLeave}
  >
    {#each paragraphs as paragraph (paragraph)}
      <p>{paragraph}</p>
    {/each}
  </div>
{/if}
