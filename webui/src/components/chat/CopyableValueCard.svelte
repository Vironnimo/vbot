<script>
  // Hover card revealing the complete value behind a shortened or clipped
  // chat value, with a Copy action: Tool row arguments (paths, commands),
  // Sub-Agent task text, background Bash commands, queued messages. Place it
  // inside the anchor element, its parent, like any `use:floatingHoverCard`
  // card. `whenTruncated` opens it only while the anchor clips its text, for
  // values shown in full whenever there is room; a character-shortened value
  // passes `false`. An optional `title` leads the card (the description a Bash
  // command stands for). The value keeps its line breaks and scrolls when
  // long; the Copy action stays in view.
  import { floatingHoverCard } from '$lib/tooltip.js';

  import CopyButton from '../ui/CopyButton.svelte';

  let {
    value = '',
    title = '',
    mono = false,
    copyLabel = '',
    copiedLabel = '',
    whenTruncated = false,
    showDelayMs = undefined,
    placement = 'top',
  } = $props();
</script>

{#if value}
  <span
    class="floating-tooltip copyable-value-card"
    use:floatingHoverCard={{ showDelayMs, placement, whenTruncated }}
  >
    <span class="copyable-value-card__body">
      {#if title}
        <span class="copyable-value-card__title">{title}</span>
      {/if}
      <span
        class="copyable-value-card__value"
        class:copyable-value-card__value--mono={mono}>{value}</span
      >
    </span>
    <CopyButton
      text={value}
      class="copyable-value-card__copy"
      label={copyLabel}
      {copiedLabel}
    />
  </span>
{/if}

<style>
  .copyable-value-card {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    max-width: min(460px, calc(100vw - 16px));
    padding: 5px 5px 5px 9px;
    overflow: hidden;
    cursor: auto;
    white-space: normal;
  }

  .copyable-value-card__body {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 3px;
    padding-top: 2px;
  }

  .copyable-value-card__title {
    font-size: var(--fs-body-sm);
    font-weight: 600;
    line-height: 1.35;
    white-space: pre-line;
  }

  .copyable-value-card__value {
    max-height: min(300px, 45vh);
    overflow: auto;
    line-height: 1.45;
    user-select: text;
    white-space: pre-wrap;
  }

  .copyable-value-card__title + .copyable-value-card__value {
    color: var(--text-med);
  }

  .copyable-value-card__value--mono {
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
  }

  .copyable-value-card :global(.copyable-value-card__copy) {
    flex: 0 0 auto;
    width: 24px;
    height: 24px;
    min-height: 24px;
    padding: 0;
  }
</style>
