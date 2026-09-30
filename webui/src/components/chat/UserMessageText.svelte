<script>
  // A user message's text bubble with safe links. A very long text (pasted
  // logs, files) starts clamped to its first lines under a fading edge, with
  // Show more/Show less below; shorter texts render in full, so the control
  // never reveals just a line or two.
  import { linkifiedTextSegments } from '$lib/markdown.js';
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';

  // Clamped bubbles show CLAMPED_LINES lines (see the CSS); only texts longer
  // than COLLAPSIBLE_LINES get clamped at all.
  const COLLAPSIBLE_LINES = 20;
  const FALLBACK_LINE_HEIGHT_PX = 24;

  let { text, open = false, onOpenChange = () => {} } = $props();

  let bubble = $state();
  let collapsible = $state(false);
  let clamped = $derived(collapsible && !open);

  function measure() {
    if (!bubble) return;
    const style = getComputedStyle(bubble);
    const lineHeight = parseFloat(style.lineHeight) || FALLBACK_LINE_HEIGHT_PX;
    const padding =
      (parseFloat(style.paddingTop) || 0) +
      (parseFloat(style.paddingBottom) || 0);
    collapsible =
      bubble.scrollHeight - padding > lineHeight * COLLAPSIBLE_LINES;
  }

  $effect(() => {
    text;
    const node = bubble;
    if (!node) return undefined;
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    // Width changes rewrap the text; the clamp itself never changes
    // scrollHeight, so this cannot oscillate.
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  });
</script>

<p
  bind:this={bubble}
  class="msg-body-text msg-body-text--user"
  class:msg-body-text--clamped={clamped}
>
  {#each linkifiedTextSegments(text) as segment, segmentIndex (segmentIndex)}
    {#if segment.href}
      <a href={segment.href} target="_blank" rel="noopener noreferrer"
        >{segment.text}</a
      >
    {:else}
      {segment.text}
    {/if}
  {/each}
</p>
{#if collapsible}
  <Button
    variant="tertiary"
    class="msg-clamp-toggle"
    aria-expanded={open}
    onClick={() => onOpenChange(!open)}
  >
    {open ? t('chat.showLess') : t('chat.showMore')}
  </Button>
{/if}
