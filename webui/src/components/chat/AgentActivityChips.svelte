<script>
  import { onMount, untrack } from 'svelte';

  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';

  // Chips collapse to their status dot at the same width as the Chat header's
  // mobile layout.
  const COMPACT_MEDIA_QUERY = '(max-width: 640px)';
  // Sub-pixel tolerance for fractional chip widths.
  const FIT_TOLERANCE_PX = 0.5;

  let {
    // Agents to show, in display order:
    // { id, name, status, unreadCount, label, tooltip }.
    agents = [],
    disabled = false,
    onSelect = () => {},
    // Opens the full Agent picker for the chips that do not fit.
    onShowMore = () => {},
  } = $props();

  let rootElement = $state();
  let measureElement = $state();
  let compact = $state(false);
  let visibleCount = $state(Number.POSITIVE_INFINITY);
  // Natural width of every chip together. The row's flex basis stays at this
  // width however many chips are shown, so hiding chips never shrinks the
  // space they are measured against.
  let naturalWidth = $state(0);

  let visibleAgents = $derived(agents.slice(0, visibleCount));
  let hiddenAgents = $derived(agents.slice(visibleCount));
  let moreLabel = $derived(
    hiddenAgents.length === 1
      ? t('chat.agentChips.moreOne', '1 more agent with activity')
      : t('chat.agentChips.more', '{count} more agents with activity', {
          count: hiddenAgents.length,
        }),
  );
  let moreTooltip = $derived(
    hiddenAgents.map((agent) => agent.label).join('\n'),
  );

  function fittingChipCount(widths, gap, moreWidth, available) {
    const total =
      widths.reduce((sum, width) => sum + width, 0) +
      gap * Math.max(0, widths.length - 1);
    if (total <= available + FIT_TOLERANCE_PX) {
      return widths.length;
    }
    let used = 0;
    let count = 0;
    for (const width of widths) {
      const next = used + (count > 0 ? gap : 0) + width;
      if (next + gap + moreWidth > available + FIT_TOLERANCE_PX) {
        break;
      }
      used = next;
      count += 1;
    }
    return count;
  }

  function measure() {
    if (!rootElement || !measureElement) {
      return;
    }
    const widths = Array.from(
      measureElement.querySelectorAll('[data-measure-chip]'),
      (element) => element.getBoundingClientRect().width,
    );
    const moreWidth =
      measureElement
        .querySelector('[data-measure-more]')
        ?.getBoundingClientRect().width ?? 0;
    const chipGap = cssPixels(getComputedStyle(measureElement).columnGap);
    const rootStyle = getComputedStyle(rootElement);
    // The row pads room for the chips' focus rings.
    const padding =
      cssPixels(rootStyle.paddingLeft) + cssPixels(rootStyle.paddingRight);
    naturalWidth = Math.ceil(
      widths.reduce((sum, width) => sum + width, 0) +
        chipGap * Math.max(0, widths.length - 1) +
        padding,
    );
    visibleCount = fittingChipCount(
      widths,
      chipGap,
      moreWidth,
      rootElement.getBoundingClientRect().width - padding,
    );
  }

  function cssPixels(value) {
    const pixels = Number.parseFloat(value);
    return Number.isFinite(pixels) ? pixels : 0;
  }

  onMount(() => {
    const compactQuery =
      typeof window.matchMedia === 'function'
        ? window.matchMedia(COMPACT_MEDIA_QUERY)
        : null;
    const updateCompact = () => {
      compact = Boolean(compactQuery?.matches);
    };
    updateCompact();
    compactQuery?.addEventListener?.('change', updateCompact);
    return () => {
      compactQuery?.removeEventListener?.('change', updateCompact);
    };
  });

  // Re-fit whenever the available space or the chips' own widths change.
  $effect(() => {
    if (
      !rootElement ||
      !measureElement ||
      typeof ResizeObserver === 'undefined'
    ) {
      return undefined;
    }
    const observer = new ResizeObserver(() => measure());
    observer.observe(rootElement);
    observer.observe(measureElement);
    return () => observer.disconnect();
  });

  $effect(() => {
    // Track the rendered content, then measure the updated DOM.
    void rootElement;
    void measureElement;
    agents.map(
      (agent) => `${agent.id}\u0000${agent.name}\u0000${agent.unreadCount}`,
    );
    void compact;
    untrack(measure);
  });
</script>

{#if agents.length > 0}
  <div
    bind:this={rootElement}
    class="agent-chips"
    class:agent-chips--compact={compact}
    role="group"
    aria-label={t('chat.agentChips.label', 'Other agents with activity')}
    style:flex-basis={naturalWidth > 0 ? `${naturalWidth}px` : undefined}
  >
    {#each visibleAgents as agent (agent.id)}
      <button
        type="button"
        class="agent-chip agent-chip--{agent.status}"
        {disabled}
        aria-label={agent.label}
        use:tooltip={agent.tooltip}
        onclick={() => onSelect(agent.id)}
      >
        <span
          class="agent-chip__dot tab-indicator tab-indicator--{agent.status}"
          aria-hidden="true"
        ></span>
        {#if !compact}
          <span class="agent-chip__name">{agent.name}</span>
          {#if agent.unreadCount > 0}
            <span class="count-badge">{agent.unreadCount}</span>
          {/if}
        {/if}
      </button>
    {/each}
    {#if hiddenAgents.length > 0}
      <button
        type="button"
        class="agent-chip agent-chip--more"
        {disabled}
        aria-haspopup="listbox"
        aria-label={moreLabel}
        use:tooltip={moreTooltip}
        onclick={() => onShowMore()}
      >
        +{hiddenAgents.length}
      </button>
    {/if}

    <!-- Invisible copy of every chip: its widths decide how many fit. -->
    <div
      bind:this={measureElement}
      class="agent-chips__measure"
      aria-hidden="true"
      inert
    >
      {#each agents as agent (agent.id)}
        <span class="agent-chip" data-measure-chip>
          <span class="agent-chip__dot tab-indicator"></span>
          {#if !compact}
            <span class="agent-chip__name">{agent.name}</span>
            {#if agent.unreadCount > 0}
              <span class="count-badge">{agent.unreadCount}</span>
            {/if}
          {/if}
        </span>
      {/each}
      <span class="agent-chip agent-chip--more" data-measure-more
        >+{agents.length}</span
      >
    </div>
  </div>
{/if}

<style>
  .agent-chips {
    position: relative;
    display: flex;
    /* Chips give up space before the Agent picker beside them does. */
    flex: 0 1000 auto;
    min-width: 0;
    align-items: center;
    gap: 6px;
    /* Room for focus rings inside the clipped row, without moving it. */
    margin: -4px;
    padding: 4px;
    overflow: hidden;
  }

  .agent-chips__measure {
    position: absolute;
    top: 4px;
    left: 4px;
    display: flex;
    width: max-content;
    gap: 6px;
    pointer-events: none;
    visibility: hidden;
  }

  .agent-chip {
    display: inline-flex;
    max-width: 180px;
    height: 28px;
    flex-shrink: 0;
    align-items: center;
    gap: 6px;
    padding: 0 10px;
    border: 0;
    border-radius: 999px;
    color: var(--text-med);
    background: var(--surface-2);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-weight: 500;
    white-space: nowrap;
    transition:
      background 120ms ease,
      color 120ms ease;
  }

  .agent-chip:hover:not(:disabled) {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  .agent-chip:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .agent-chip:disabled {
    cursor: default;
    opacity: 0.55;
  }

  .agent-chip__name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .agent-chip--more {
    font-variant-numeric: tabular-nums;
  }

  /* Phone widths: dot-only chips with full touch targets; the name stays in
     the accessible label and tooltip. */
  .agent-chips--compact {
    gap: 4px;
  }

  .agent-chips--compact .agent-chips__measure {
    gap: 4px;
  }

  .agent-chips--compact .agent-chip {
    width: 36px;
    height: 36px;
    justify-content: center;
    padding: 0;
  }

  .agent-chips--compact .agent-chip__dot {
    width: 8px;
    height: 8px;
  }

  .agent-chips--compact .agent-chip--more {
    width: auto;
    min-width: 36px;
    padding: 0 8px;
  }
</style>
