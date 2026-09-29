<script>
  // Inline change summary ("3 files changed, +12 -4") whose hover card lists
  // every changed file with its own line counts and a Copy action for its
  // full path. Shared by the Run footer and the Session information panel.
  import {
    changeStatsParts,
    changedFilesCard,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard } from '$lib/tooltip.js';

  import CopyButton from '../ui/CopyButton.svelte';

  let { stats = null, placement = 'top', class: className = '' } = $props();

  let parts = $derived(changeStatsParts(stats));
  let card = $derived(changedFilesCard(stats));
  // The file list renders on the first hover, focus or press: a long timeline
  // holds many summaries whose cards are never opened.
  let activated = $state(false);

  const activate = () => {
    activated = true;
  };

  // Zero line counts carry no color: nothing was added or removed.
  const isZeroCount = (kind) =>
    (kind === 'added' && stats.added === 0) ||
    (kind === 'removed' && stats.removed === 0);

  const unlistedLabel = (count) =>
    count === 1
      ? t('chat.changedFiles.unlistedOne')
      : t('chat.changedFiles.unlistedMany', { count });
</script>

{#if parts.length > 0}
  <!-- The summary is focusable so keyboard users reach the file card. -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_static_element_interactions -->
  <span
    class="change-stats {className}"
    class:change-stats--has-card={card}
    tabindex={card ? 0 : undefined}
    onpointerenter={activate}
    onpointerdown={activate}
    onfocusin={activate}
  >
    {#each parts as part (part.kind)}
      <span
        class="change-stats__part change-stats__part--{part.kind}"
        class:change-stats__part--zero={isZeroCount(part.kind)}
        >{part.text}</span
      >
    {/each}
    {#if card}
      <div
        class="floating-card changed-files-card"
        use:floatingHoverCard={{ placement, openOnPress: true }}
      >
        {#if activated}
          <div class="changed-files-card__header">
            <div class="changed-files-card__heading">
              <span class="changed-files-card__title">{card.title}</span>
              <span class="changed-files-card__totals">
                <span
                  class="changed-files-card__added"
                  class:changed-files-card__total--zero={card.added === 0}
                  >+{card.added}</span
                >
                <span
                  class="changed-files-card__removed"
                  class:changed-files-card__total--zero={card.removed === 0}
                  >-{card.removed}</span
                >
              </span>
            </div>
            {#if card.rootSegments.length > 0}
              <!-- Break the shared directory only after a separator. -->
              <div class="changed-files-card__root">
                {#each card.rootSegments as segment, index (index)}{#if index > 0}<wbr
                    />{/if}{segment}{/each}
              </div>
            {/if}
          </div>
          <ul class="changed-files-card__list">
            {#each card.rows as row (row.path)}
              <li class="changed-files-card__row">
                <span class="changed-files-card__file">
                  <span class="changed-files-card__name">{row.name}</span>
                  {#if row.directory}
                    <!-- Long folders lose their start, keeping the nearest
                         folder names visible. -->
                    <span class="changed-files-card__directory"
                      ><bdi>{row.directory}</bdi></span
                    >
                  {/if}
                </span>
                {#if row.added === null || row.removed === null}
                  <span class="changed-files-card__count"></span>
                  <span class="changed-files-card__count"></span>
                {:else}
                  <span
                    class="changed-files-card__count changed-files-card__count--added"
                    class:changed-files-card__count--zero={row.added === 0}
                    >+{row.added}</span
                  >
                  <span
                    class="changed-files-card__count changed-files-card__count--removed"
                    class:changed-files-card__count--zero={row.removed === 0}
                    >-{row.removed}</span
                  >
                {/if}
                <CopyButton
                  text={row.path}
                  class="changed-files-card__copy"
                  label={t('chat.changedFiles.copyPath', { name: row.name })}
                  copiedLabel={t('chat.changedFiles.pathCopied')}
                />
              </li>
            {/each}
          </ul>
          {#if card.unlisted > 0}
            <p class="changed-files-card__unlisted">
              {unlistedLabel(card.unlisted)}
            </p>
          {/if}
        {/if}
      </div>
    {/if}
  </span>
{/if}

<style>
  /* One unit, so the label and its counts never wrap apart. */
  .change-stats {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border-radius: var(--r-sm);
    white-space: nowrap;
    cursor: default;
  }

  .change-stats:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .change-stats--has-card .change-stats__part--files {
    text-decoration: underline dotted;
    text-decoration-color: var(--text-faint);
    text-underline-offset: 3px;
  }

  .change-stats--has-card:hover .change-stats__part--files {
    color: var(--text-hi);
    text-decoration-color: currentColor;
  }

  .change-stats__part--added {
    color: var(--green);
  }

  .change-stats__part--removed {
    color: var(--red);
  }

  .change-stats__part--zero {
    color: inherit;
  }

  /* The card is portaled to <body>; its scoped classes still apply. The
     header and the unlisted note stay in place while a long list scrolls. */
  .changed-files-card.floating-card {
    display: flex;
    flex-direction: column;
    width: max-content;
    min-width: min(280px, calc(100vw - 16px));
    max-width: min(480px, calc(100vw - 16px));
    padding: 10px 12px 8px;
    overflow: hidden;
    overflow-wrap: normal;
    white-space: normal;
    cursor: default;
  }

  .changed-files-card__header {
    flex: 0 0 auto;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }

  .changed-files-card__heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
  }

  .changed-files-card__title {
    color: var(--text-hi);
    font-weight: 600;
  }

  .changed-files-card__totals {
    display: flex;
    gap: 6px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .changed-files-card__added {
    color: var(--green);
  }

  .changed-files-card__removed {
    color: var(--red);
  }

  .changed-files-card__total--zero {
    color: var(--text-lo);
  }

  /* The file rows set the card width; a long shared directory wraps below
     the heading instead of widening the card. */
  .changed-files-card__root {
    contain: inline-size;
    margin-top: 2px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  /* One grid for every row, so the counts line up in columns. */
  .changed-files-card__list {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto auto;
    min-height: 0;
    margin: 6px -6px 0;
    padding: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    list-style: none;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.45;
  }

  .changed-files-card__row {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: center;
    column-gap: 10px;
    min-height: 26px;
    padding: 0 2px 0 6px;
    border-radius: var(--r-md);
  }

  .changed-files-card__row:hover,
  .changed-files-card__row:focus-within {
    background: var(--surface-3);
  }

  .changed-files-card__file {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
    white-space: nowrap;
  }

  .changed-files-card__name {
    flex: 0 0 auto;
    max-width: 100%;
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
  }

  .changed-files-card__directory {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-lo);
    text-overflow: ellipsis;
    direction: rtl;
  }

  .changed-files-card__count {
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
  }

  .changed-files-card__count--added {
    color: var(--green);
  }

  .changed-files-card__count--removed {
    color: var(--red);
  }

  .changed-files-card__count--zero {
    color: var(--text-lo);
  }

  /* The Copy action appears with its row; touch has no hover, so it stays.
     Pointer precision gets a compact square; phones keep the touch target. */
  .changed-files-card__row :global(.changed-files-card__copy) {
    opacity: 0;
    transition: opacity 120ms ease;
  }

  @media (min-width: 641px) {
    .changed-files-card__row
      :global(.btn-tertiary.btn-icon.changed-files-card__copy) {
      width: 22px;
      height: 22px;
      min-height: 22px;
      border-radius: var(--r-sm);
    }
  }

  .changed-files-card__row:hover :global(.changed-files-card__copy),
  .changed-files-card__row:focus-within :global(.changed-files-card__copy) {
    opacity: 1;
  }

  @media (hover: none) {
    .changed-files-card__row :global(.changed-files-card__copy) {
      opacity: 1;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .changed-files-card__row :global(.changed-files-card__copy) {
      transition: none;
    }
  }

  .changed-files-card__unlisted {
    flex: 0 0 auto;
    margin: 6px 0 0;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
  }
</style>
