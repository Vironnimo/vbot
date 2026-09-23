<script>
  // A Tool row's primary argument values, shared by the Run timeline
  // (ChatAssistantRun) and single timeline entries (ChatTimelineEntry).
  // Truncated values expose their complete text through the quick tooltip;
  // copyable values use a hover card with a Copy action instead.
  import { t } from '$lib/i18n.js';
  import {
    INTENTIONAL_HOVER_SHOW_DELAY_MS,
    floatingHoverCard,
    tooltip,
  } from '$lib/tooltip.js';

  import CopyButton from '../ui/CopyButton.svelte';

  let { primary = [] } = $props();
</script>

<span class="te-arg te-primary">
  <span class="te-primary-values">
    {#each primary as part, index (`${part.kind}:${index}`)}
      {#if index > 0}<span class="te-primary-separator">·</span>{/if}
      <!-- The truncated value itself must receive focus so its complete
           plain-text value reaches keyboard users. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <span
        class="te-arg-value te-primary-value te-primary-value--{part.truncate}"
        tabindex={part.tooltipText ? 0 : undefined}
        use:tooltip={part.copyable ? '' : part.tooltipText}
      >
        {part.text}{#if part.copyable && part.tooltipText}
          <span
            class="floating-tooltip tool-primary-hover-card"
            use:floatingHoverCard={{
              showDelayMs: INTENTIONAL_HOVER_SHOW_DELAY_MS,
            }}
          >
            <span class="tool-primary-hover-card__value">{part.fullText}</span>
            <CopyButton
              text={part.fullText}
              class="tool-primary-hover-card__copy"
              label={t('chat.copyToolValue', 'Copy full value')}
              copiedLabel={t('chat.toolValueCopied', 'Full value copied')}
            />
          </span>
        {/if}</span
      >
    {/each}
  </span>
</span>
