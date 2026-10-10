<script>
  // A Session's context-window readout, shared by the Chat composer and Swarm
  // Activity: a ring filled to the share of the context window that Current
  // Context Usage takes, and a hover card with the exact figures, the usage
  // breakdown (`contextUsageCardModel`) and, when the host wires one, the
  // Compaction action. The composer shows the ring alone; `showFigures` adds
  // the compact figures beside it ("51K / 1M · 5%"), and the button's name
  // then carries them after the ring's label, so it contains the visible text.
  // Renders nothing until the tokens and the window are known.
  import { floatingHoverCard } from '$lib/tooltip.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { formatTokens } from '$lib/statisticsView.js';
  import {
    contextLimitWarning,
    contextUsageCardModel,
  } from '$lib/tokenUsageTooltip.js';
  import Button from '../ui/Button.svelte';

  let {
    contextUsage = null,
    contextWindow = null,
    usage = null,
    sessionUsage = null,
    // The Session's effective Compaction Policy (null while unknown).
    compactionPolicy = null,
    showFigures = false,
    // The Compaction action; without one the card has no footer.
    onForceCompaction = null,
    compactionState = 'unavailable',
    compactionSubmitting = false,
    disabled = false,
  } = $props();

  const RING_RADIUS = 6;
  const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

  let fillRatio = $derived.by(() => {
    const tokens = contextUsage?.tokens;
    if (
      !Number.isFinite(tokens) ||
      !Number.isFinite(contextWindow) ||
      contextWindow <= 0
    ) {
      return null;
    }
    return Math.min(1, tokens / contextWindow);
  });
  let ringOffset = $derived(
    fillRatio === null
      ? RING_CIRCUMFERENCE
      : RING_CIRCUMFERENCE * (1 - fillRatio),
  );
  let card = $derived(
    contextUsageCardModel(contextUsage, usage, sessionUsage, contextWindow),
  );
  // Fill level of the ring and meter, relative to the Session's automatic
  // Compaction trigger. Current Context Usage tokens are the same projection
  // the server's trigger evaluates; see `contextLimitWarning`.
  let warning = $derived(
    contextLimitWarning(fillRatio, contextWindow, compactionPolicy),
  );
  let level = $derived(warning.level);
  let percentLabel = $derived.by(() => {
    if (fillRatio === null) {
      return '';
    }
    if (fillRatio > 0 && fillRatio < 0.01) {
      return t('chat.contextBelowOnePercent');
    }
    return new Intl.NumberFormat(activeLocaleTag(), {
      style: 'percent',
      maximumFractionDigits: 0,
    }).format(fillRatio);
  });
  let figures = $derived(
    showFigures && fillRatio !== null
      ? t('chat.contextRingFigures', {
          tokens: formatTokens(contextUsage.tokens, activeLocaleTag()),
          context: formatTokens(contextWindow, activeLocaleTag()),
          share: percentLabel,
        })
      : '',
  );
  // One action for both paths: an active Run compacts at its next safe step
  // ("Compaction requested..."), otherwise a manual Compaction Run starts.
  let compactionLabel = $derived(
    compactionState === 'running'
      ? t('chat.compactionRunning')
      : compactionState === 'pending' || compactionSubmitting
        ? t('chat.compactionPending')
        : t('chat.compactNow'),
  );
  let compactionDisabled = $derived(
    disabled || compactionSubmitting || compactionState !== 'idle',
  );
</script>

{#if fillRatio !== null}
  <span class="context-ring context-ring--{level}">
    <button
      type="button"
      class="context-ring-trigger"
      class:context-ring-trigger--figures={figures}
      aria-label={figures
        ? t('chat.contextRingFiguresLabel', { figures })
        : t('chat.contextRingLabel')}
    >
      <svg
        class="context-ring__icon"
        viewBox="0 0 16 16"
        width="16"
        height="16"
        aria-hidden="true"
      >
        <circle
          class="context-ring__track"
          cx="8"
          cy="8"
          r={RING_RADIUS}
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
        />
        <circle
          class="context-ring__fill"
          cx="8"
          cy="8"
          r={RING_RADIUS}
          fill="none"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-dasharray={RING_CIRCUMFERENCE}
          stroke-dashoffset={ringOffset}
          transform="rotate(-90 8 8)"
        />
      </svg>
      {#if figures}<span class="context-ring__figures">{figures}</span>{/if}
    </button>
    <div
      class="floating-card context-card context-card--{level}"
      use:floatingHoverCard={{ openOnPress: true }}
    >
      <div class="context-card__header">
        <span class="context-card__title">{t('chat.contextCardTitle')}</span>
        <span class="context-card__figures">
          {#if card.summary}<span class="context-card__usage"
              >{card.summary}</span
            >{/if}
          <span class="context-card__percent">{percentLabel}</span>
        </span>
      </div>
      <span class="context-card__meter" aria-hidden="true">
        <span
          class="context-card__meter-fill"
          style:width={`${Math.round(fillRatio * 1000) / 10}%`}
        ></span>
      </span>
      {#if warning.message}
        <p class="context-card__level">{warning.message}</p>
      {/if}
      {#each card.sections as section (section.id)}
        <section class="context-card__section">
          {#if section.title}
            <h3 class="context-card__section-title">{section.title}</h3>
          {/if}
          <dl class="context-card__rows">
            {#each section.rows as row, index (index)}
              <div
                class="context-card__row"
                class:context-card__row--sub={row.sub}
              >
                <dt>{row.label}</dt>
                <dd>{row.value}</dd>
              </div>
            {/each}
          </dl>
        </section>
      {/each}
      {#if onForceCompaction}
        <div class="context-card__footer">
          <Button
            variant="secondary"
            class="context-card__action"
            disabled={compactionDisabled}
            onClick={onForceCompaction}
          >
            {compactionLabel}
          </Button>
        </div>
      {/if}
    </div>
  </span>
{/if}

<style>
  /* The host places the ring (the composer: bottom-right of the input box,
     composer.css); the ring itself only lines up its trigger. */
  .context-ring {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
  }

  .context-ring-trigger {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    border-radius: 50%;
    color: var(--text-lo);
    background: transparent;
  }

  .context-ring-trigger:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  /* With figures the trigger is a borderless tertiary-style pill: the ring
     keeps its colors and the figures read as secondary text. */
  .context-ring-trigger--figures {
    gap: 6px;
    width: auto;
    height: 28px;
    padding: 0 8px 0 6px;
    border-radius: var(--r-md);
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    transition:
      color 120ms ease,
      background 120ms ease;
  }

  .context-ring-trigger--figures:hover {
    color: var(--text-hi);
    background: var(--surface-3);
  }

  /* The track is faint; the fill arc shows how much of the window is used. */
  .context-ring__icon {
    flex-shrink: 0;
    color: var(--text-lo);
  }

  .context-ring__track {
    opacity: 0.3;
  }

  .context-ring__fill {
    color: var(--text-med);
    transition:
      stroke-dashoffset 300ms ease,
      color 300ms ease;
  }

  .context-ring--high .context-ring__fill {
    color: var(--amber);
  }

  .context-ring--critical .context-ring__fill {
    color: var(--red);
  }

  /* The card on the shared floating card (styles/app/hints.css), portaled to
     <body>. The meter repeats the ring's track and fill colors. */
  .context-card.floating-card {
    width: min(300px, calc(100vw - 16px));
  }

  .context-card__header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }

  .context-card__title {
    color: var(--text-hi);
    font-weight: 600;
  }

  .context-card__figures {
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .context-card__usage {
    color: var(--text-hi);
  }

  .context-card__percent {
    color: var(--text-med);
  }

  .context-card__meter {
    display: block;
    height: 4px;
    margin-top: 8px;
    overflow: hidden;
    border-radius: 999px;
    background: color-mix(in srgb, var(--text-lo) 30%, transparent);
  }

  .context-card__meter-fill {
    display: block;
    height: 100%;
    border-radius: inherit;
    background: var(--text-med);
  }

  .context-card--high .context-card__meter-fill {
    background: var(--amber);
  }

  .context-card--critical .context-card__meter-fill {
    background: var(--red);
  }

  .context-card__level {
    margin: 8px 0 0;
    font-size: var(--fs-label-sm);
    font-weight: 500;
  }

  .context-card--high .context-card__level {
    color: var(--amber);
  }

  .context-card--critical .context-card__level {
    color: var(--red);
  }

  /* Breakdown: labelled figures in a right-aligned tabular column; indented
     sub-rows are shares of the row above, never additions. */
  .context-card__section {
    margin-top: 12px;
  }

  .context-card__section + .context-card__section {
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }

  .context-card__section-title {
    margin: 0 0 4px;
    color: var(--text-hi);
    font-size: var(--fs-label-sm);
    font-weight: 600;
  }

  .context-card__rows {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin: 0;
  }

  .context-card__row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    font-size: var(--fs-label-sm);
    line-height: 1.5;
  }

  .context-card__row dt {
    min-width: 0;
    color: var(--text-med);
  }

  .context-card__row dd {
    margin: 0;
    color: var(--text-hi);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .context-card__row--sub dt {
    padding-left: 12px;
    color: var(--text-lo);
  }

  .context-card__row--sub dd {
    color: var(--text-med);
  }

  .context-card__footer {
    display: flex;
    justify-content: flex-end;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }

  /* Compact action with pointer precision; phones keep the 40px touch target. */
  @media (min-width: 641px) {
    .context-card__footer :global(.btn-secondary.context-card__action) {
      min-height: 28px;
      padding: 4px 11px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .context-ring__fill,
    .context-ring-trigger--figures {
      transition: none;
    }
  }
</style>
