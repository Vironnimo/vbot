<script>
  // The "?" info dot — the explanatory tier of the hint system (quick label
  // tooltips are `use:tooltip` from lib/tooltip.js). Hover previews the
  // popover, click/tap pins it (touch has no hover), Escape and outside
  // clicks dismiss. Callers pass already-translated `text`; blank lines
  // separate paragraphs.
  import { onDestroy } from 'svelte';

  import { portal } from '../../lib/dropdownPanel.js';
  import { t } from '../../lib/i18n.js';
  import { positionFloating } from '../../lib/tooltip.js';

  let { text = '', ariaLabel = '', class: className = '' } = $props();

  const CLOSE_GRACE_MS = 150;

  let open = $state(false);
  let pinned = $state(false);
  let dotElement = $state(null);
  let popoverElement = $state(null);
  let closeTimer = null;

  let label = $derived(ariaLabel || t('common.moreInfo', 'More information'));
  let paragraphs = $derived(
    String(text)
      .split(/\n{2,}/)
      .map((paragraph) => paragraph.trim())
      .filter(Boolean),
  );

  function cancelScheduledClose() {
    if (closeTimer !== null) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
  }

  function openPopover() {
    cancelScheduledClose();
    open = true;
  }

  function close() {
    cancelScheduledClose();
    open = false;
    pinned = false;
  }

  function scheduleClose() {
    if (pinned) {
      return;
    }
    cancelScheduledClose();
    closeTimer = setTimeout(() => {
      closeTimer = null;
      open = false;
    }, CLOSE_GRACE_MS);
  }

  function onDotClick() {
    if (pinned) {
      close();
    } else {
      pinned = true;
      openPopover();
    }
  }

  function onWindowKeydown(event) {
    if (event.key === 'Escape') {
      close();
    }
  }

  function onWindowPointerdown(event) {
    const target = event.target instanceof Node ? event.target : null;
    if (
      target &&
      (dotElement?.contains(target) || popoverElement?.contains(target))
    ) {
      return;
    }
    close();
  }

  $effect(() => {
    if (open && popoverElement && dotElement) {
      positionFloating(dotElement, popoverElement);
      window.addEventListener('keydown', onWindowKeydown, true);
      window.addEventListener('pointerdown', onWindowPointerdown, true);
      window.addEventListener('scroll', close, true);
      return () => {
        window.removeEventListener('keydown', onWindowKeydown, true);
        window.removeEventListener('pointerdown', onWindowPointerdown, true);
        window.removeEventListener('scroll', close, true);
      };
    }
  });

  onDestroy(cancelScheduledClose);
</script>

<button
  bind:this={dotElement}
  type="button"
  class={['info-hint', className].filter(Boolean).join(' ')}
  aria-label={label}
  aria-expanded={open}
  aria-describedby={open ? 'info-hint-popover' : undefined}
  onpointerenter={openPopover}
  onpointerleave={scheduleClose}
  onfocus={openPopover}
  onblur={scheduleClose}
  onclick={onDotClick}>?</button
>

{#if open && paragraphs.length > 0}
  <div
    bind:this={popoverElement}
    use:portal
    class="info-popover"
    id="info-hint-popover"
    role="tooltip"
    onpointerenter={cancelScheduledClose}
    onpointerleave={scheduleClose}
  >
    {#each paragraphs as paragraph (paragraph)}
      <p>{paragraph}</p>
    {/each}
  </div>
{/if}
