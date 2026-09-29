<script>
  // A Tool row's primary argument values, shared by the Run timeline
  // (ChatAssistantRun) and single timeline entries (ChatTimelineEntry).
  // A value whose complete text is hidden (shortened, clipped by the row, or
  // standing for a command) reveals it through the quick tooltip, or through
  // a hover card with a Copy action when the value is copyable.
  import { INTENTIONAL_HOVER_SHOW_DELAY_MS, tooltip } from '$lib/tooltip.js';

  import CopyableValueCard from './CopyableValueCard.svelte';

  let { primary = [] } = $props();

  function quickTooltip(reveal) {
    if (!reveal || reveal.copy) {
      return '';
    }
    return {
      title: reveal.title,
      text: reveal.value,
      mono: reveal.mono,
      whenTruncated: reveal.whenTruncated,
    };
  }
</script>

<span class="te-arg te-primary">
  <span class="te-primary-values">
    {#each primary as part, index (`${part.kind}:${index}`)}
      {#if index > 0}<span class="te-primary-separator">·</span>{/if}
      <!-- The value itself must receive focus so its complete plain-text
           value reaches keyboard users. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <span
        class="te-arg-value te-primary-value te-primary-value--{part.truncate}"
        tabindex={part.reveal ? 0 : undefined}
        use:tooltip={quickTooltip(part.reveal)}
      >
        {part.text}{#if part.reveal?.copy}
          <CopyableValueCard
            value={part.reveal.value}
            title={part.reveal.title}
            mono={part.reveal.mono}
            whenTruncated={part.reveal.whenTruncated}
            copyLabel={part.reveal.copy.label}
            copiedLabel={part.reveal.copy.copiedLabel}
            showDelayMs={INTENTIONAL_HOVER_SHOW_DELAY_MS}
          />
        {/if}</span
      >
    {/each}
  </span>
</span>
