<script>
  // The Wiki's page list: one search field, the deleted-pages switch and the
  // pages, newest change first. WikiPanel owns the list state and requests;
  // this component shows it and reports the user's choices.
  import { t } from '../../../../webui/src/lib/i18n.js';
  import { isImeComposing } from '../../../../webui/src/lib/keyboard.js';
  import {
    formatMoment,
    formatRelativeTime,
  } from '../../../../webui/src/lib/timeText.js';
  import { tooltip } from '../../../../webui/src/lib/tooltip.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import { authorLabel } from './pagePresentation.js';

  let {
    entries,
    loaded,
    more,
    query,
    shownQuery,
    wordsMatch,
    includeDeleted,
    error,
    selectedId,
    now,
    onQuery,
    onSearch,
    onClearSearch,
    onIncludeDeleted,
    onOpen,
    onLoadMore,
    onCreate,
    onRetry,
  } = $props();

  let root = $state(null);

  /** Focuses the row of page `id`, or the search field without one. */
  export function focusPage(id) {
    const rows = root?.querySelectorAll('.wiki-row') ?? [];
    const row = [...rows].find((item) => item.dataset.pageId === id);
    (row ?? root?.querySelector('#wiki-search'))?.focus();
  }

  // Participants refer to a page by its number, as in "w3" or a "#wiki/w3" link.
  const pageRef = (page) => (page.number != null ? `w${page.number}` : '');

  // A row names the page and when it last changed; the tooltip adds the
  // latest revision, its author and the exact moment.
  const pageTooltip = (page) => ({
    title: page.title,
    rows: [
      { label: t('swarm.wiki.revisionLabel'), value: page.revision },
      { label: t('swarm.wiki.author'), value: authorLabel(page.author) },
      { label: t('swarm.wiki.updated'), value: formatMoment(page.updated_at) },
    ].filter((row) => row.value != null && row.value !== ''),
    placement: 'right',
    alignTo: '.wiki-row__title',
  });

  const rowMeta = (page) =>
    [
      page.deleted ? t('swarm.wiki.deleted') : '',
      formatRelativeTime(page.updated_at, now),
    ]
      .filter(Boolean)
      .join(' · ');
</script>

{#snippet clearSearch()}
  <Button variant="tertiary" onClick={onClearSearch}
    >{t('swarm.wiki.clearSearch')}</Button
  >
{/snippet}

<div class="wiki-index__inner" bind:this={root}>
  <div class="wiki-index__bar">
    <div class="wiki-search">
      <svg
        viewBox="0 0 16 16"
        width="16"
        height="16"
        fill="none"
        stroke="currentColor"
        aria-hidden="true"
        ><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></svg
      >
      <!-- No form: the page sandbox blocks native form submission, so Enter
           searches through its own handler. -->
      <TextField
        id="wiki-search"
        type="search"
        value={query}
        placeholder={t('swarm.wiki.search')}
        ariaLabel={t('swarm.wiki.search')}
        autocomplete="off"
        onInput={(value) => onQuery(value)}
        onkeydown={(event) => {
          if (event.key === 'Enter' && !isImeComposing(event)) {
            event.preventDefault();
            onSearch();
          }
        }}
      />
    </div>
    <Button
      variant="secondary"
      icon
      ariaLabel={t('swarm.wiki.new')}
      tooltip={t('swarm.wiki.new')}
      onClick={onCreate}
      ><svg
        viewBox="0 0 24 24"
        width="16"
        height="16"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg
      ></Button
    >
  </div>
  <label class="wiki-index__deleted"
    ><Toggle
      size="sm"
      checked={includeDeleted}
      ariaLabel={t('swarm.wiki.showDeleted')}
      onChange={onIncludeDeleted}
    />{t('swarm.wiki.showDeleted')}</label
  >
  {#if error}<Banner variant="error" role="alert" class="wiki-index__error"
      ><p>{error}</p>
      <Button variant="tertiary" onClick={onRetry}>{t('common.retry')}</Button
      ></Banner
    >{/if}
  {#if entries.length}
    {#if wordsMatch}<p class="wiki-index__status" role="status">
        {t('swarm.wiki.matchedWords', { query: wordsMatch })}
      </p>{/if}
    <nav class="wiki-pages" aria-label={t('swarm.wiki.pages')}>
      {#each entries as page (page.page_id)}
        {@const meta = rowMeta(page)}
        <button
          type="button"
          class="secondary-list__item wiki-row"
          class:active={selectedId === page.page_id}
          class:wiki-row--deleted={page.deleted}
          aria-current={selectedId === page.page_id ? 'page' : undefined}
          data-page-id={page.page_id}
          use:tooltip={() => pageTooltip(page)}
          onclick={() => onOpen(page.page_id)}
        >
          <span class="wiki-row__number wiki-number">{pageRef(page)}</span>
          <span class="wiki-row__title">{page.title}</span>
          {#if meta}<span class="wiki-row__meta">{meta}</span>{/if}
        </button>
      {/each}
    </nav>
    {#if more}<div class="wiki-index__more">
        <Button variant="tertiary" onClick={onLoadMore}
          >{t('swarm.wiki.loadMore')}</Button
        >
      </div>{/if}
  {:else if !loaded}
    {#if !error}<p class="wiki-index__status" role="status">
        {t('common.loading')}
      </p>{/if}
  {:else if shownQuery}
    <EmptyState
      density="compact"
      class="wiki-index__empty"
      title={t('swarm.wiki.noMatches')}
      description={t('swarm.wiki.noMatchesHelp', { query: shownQuery })}
      actions={clearSearch}
    />
  {:else if !error}
    <EmptyState
      density="compact"
      class="wiki-index__empty wiki-index__empty--wiki"
      title={t('swarm.wiki.empty')}
      description={t('swarm.wiki.about')}
    />
  {/if}
</div>

<style>
  .wiki-index__inner {
    display: grid;
    gap: var(--space-sm);
    min-width: 0;
  }
  /* The search field takes the height of the New page button beside it. */
  .wiki-index__bar {
    display: flex;
    align-items: stretch;
    gap: var(--space-sm);
  }
  .wiki-search {
    position: relative;
    flex: 1;
    min-width: 0;
  }
  .wiki-search svg {
    position: absolute;
    top: 50%;
    left: 11px;
    color: var(--text-lo);
    transform: translateY(-50%);
    pointer-events: none;
  }
  .wiki-search :global(input) {
    box-sizing: border-box;
    width: 100%;
    height: 100%;
    padding-left: 34px;
  }
  .wiki-index__deleted {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
    width: fit-content;
    padding: 2px 0;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    cursor: pointer;
  }
  .wiki-index__inner :global(.wiki-index__error) {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-xs);
  }
  .wiki-pages {
    display: grid;
    gap: 2px;
    margin-top: var(--space-xs);
  }
  /* Number, then title over the time of the latest change; the number
     column keeps titles aligned. */
  .wiki-row {
    display: grid;
    grid-template-columns: 3.6ch minmax(0, 1fr);
    align-items: baseline;
    column-gap: var(--space-sm);
    row-gap: 2px;
    padding: 7px 10px;
    color: var(--text-hi);
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .wiki-row__number {
    grid-row: 1;
    grid-column: 1;
  }
  .wiki-row__title {
    grid-row: 1;
    grid-column: 2;
    overflow: hidden;
    font-size: var(--fs-body-md);
    line-height: 1.4;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .wiki-row__meta {
    grid-row: 2;
    grid-column: 2;
    overflow: hidden;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    line-height: 1.35;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .wiki-row--deleted .wiki-row__title {
    color: var(--text-med);
  }
  .wiki-number {
    color: var(--text-lo);
    font: 600 var(--fs-label-sm) / 1.4 var(--font-ui);
    font-variant-numeric: tabular-nums;
  }
  .wiki-index__more {
    display: flex;
  }
  .wiki-index__status {
    margin: var(--space-xs) 10px 0;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
  }
</style>
