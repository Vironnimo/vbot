<script>
  // Inline change summary ("3 files changed, +12 -4") whose hover card lists
  // every changed file grouped by folder, with its own line counts and a Copy
  // action for its full path.
  // Shared by the Run footer and the Session information panel.
  import {
    changeStatsParts,
    changedFilesCard,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';

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

  const folderFilesLabel = (count) =>
    count === 1
      ? t('chat.changedFiles.folderFilesOne')
      : t('chat.changedFiles.folderFilesMany', { count });

  // Name, one column per shown count kind, and the Copy action.
  const listColumns = (countKinds) =>
    ['minmax(0, 1fr)', ...countKinds.map(() => 'auto'), 'auto'].join(' ');
</script>

{#snippet counts(line, quiet)}
  {#each card.countKinds as kind (kind)}
    {#if line[kind] === null}
      <span class="changed-files-card__count"></span>
    {:else}
      <span
        class="changed-files-card__count changed-files-card__count--{kind}"
        class:changed-files-card__count--zero={quiet || line[kind] === 0}
        >{kind === 'added' ? '+' : '-'}{line[kind]}</span
      >
    {/if}
  {/each}
{/snippet}

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
          <ul
            class="changed-files-card__list"
            style:grid-template-columns={listColumns(card.countKinds)}
          >
            {#each card.groups as group (group.directory)}
              {#if group.directory}
                <li class="changed-files-card__group">
                  <span class="changed-files-card__folder">
                    <svg
                      class="changed-files-card__folder-icon"
                      viewBox="0 0 16 16"
                      width="13"
                      height="13"
                      aria-hidden="true"
                    >
                      <path
                        d="M1.75 3.5h4.1l1.5 1.6h6.9v7.4H1.75z"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linejoin="round"
                      />
                    </svg>
                    <!-- Long folders lose their start, keeping the nearest
                         folder names visible. -->
                    <span class="changed-files-card__directory"
                      ><bdi>{group.directory}</bdi></span
                    >
                    <span class="changed-files-card__folder-files"
                      >{folderFilesLabel(group.rows.length)}</span
                    >
                  </span>
                  {@render counts(group, true)}
                  <span></span>
                </li>
              {/if}
              {#each group.rows as row (row.path)}
                <li
                  class="changed-files-card__row"
                  class:changed-files-card__row--nested={group.directory}
                >
                  <span
                    class="changed-files-card__name"
                    use:tooltip={{
                      text: row.path,
                      mono: true,
                      placement: 'right',
                      whenTruncated: true,
                    }}>{row.name}</span
                  >
                  {@render counts(row, false)}
                  <CopyButton
                    text={row.path}
                    class="changed-files-card__copy"
                    label={t('chat.changedFiles.copyPath', { name: row.name })}
                    copiedLabel={t('chat.changedFiles.pathCopied')}
                  />
                </li>
              {/each}
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

  /* The card is portaled to <body>; its scoped classes still apply. It grows
     with long file names and many files up to a wide, tall cap; the header
     and the unlisted note stay in place while the list scrolls. */
  .changed-files-card.floating-card {
    display: flex;
    flex-direction: column;
    width: max-content;
    min-width: min(300px, calc(100vw - 16px));
    max-width: min(680px, calc(100vw - 16px));
    max-height: min(640px, 75vh);
    padding: 12px 14px 10px;
    overflow: hidden;
    overflow-wrap: normal;
    white-space: normal;
    cursor: default;
  }

  .changed-files-card__header {
    flex: 0 0 auto;
    padding-bottom: 9px;
    border-bottom: 1px solid var(--border);
  }

  .changed-files-card__heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 20px;
  }

  .changed-files-card__title {
    color: var(--text-hi);
    font-weight: 600;
  }

  .changed-files-card__totals {
    display: flex;
    gap: 8px;
    font-size: var(--fs-body-sm);
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
    margin-top: 3px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    line-height: 1.45;
    overflow-wrap: anywhere;
  }

  /* One grid for every row, so the counts line up in columns. */
  .changed-files-card__list {
    display: grid;
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

  .changed-files-card__group,
  .changed-files-card__row {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: center;
    column-gap: 10px;
    min-height: 26px;
    padding: 0 2px 0 8px;
    border-radius: var(--r-md);
  }

  /* A folder heads its files; the space above separates it from the
     previous folder's files. */
  .changed-files-card__group {
    margin-top: 6px;
    color: var(--text-lo);
  }

  .changed-files-card__group:first-child {
    margin-top: 0;
  }

  .changed-files-card__row--nested {
    padding-left: 27px;
  }

  .changed-files-card__row:hover,
  .changed-files-card__row:focus-within {
    background: var(--surface-3);
  }

  .changed-files-card__folder {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    white-space: nowrap;
  }

  .changed-files-card__folder-icon {
    flex: 0 0 auto;
    color: var(--text-faint);
  }

  .changed-files-card__directory {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    text-overflow: ellipsis;
    direction: rtl;
  }

  .changed-files-card__folder-files {
    flex: 0 0 auto;
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
  }

  .changed-files-card__name {
    min-width: 0;
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
    white-space: nowrap;
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
