<script>
  // One page of Skill packages, one line each: the name with problem badges
  // and, at the end, a quiet source label with who gets the package. The
  // description is never shown inline, only in the row's tooltip (explicit
  // user requirement, see the Skills section of webui/design.md). A row
  // opens the package's page; a right click or the context menu key asks
  // `onContextMenu(entry, event)` for its menu (default already prevented).
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import { skillAccessSummary, skillRowDetails } from './skillAccess.js';
  import {
    skillDiagnosticLines,
    skillSourceLabel,
    skillStatusLabel,
    skillStatusVariant,
  } from './skillsView.js';

  const noop = () => {};

  let {
    entries = [],
    total = 0,
    currentId = null,
    loading = false,
    loaded = false,
    filtersActive = false,
    emptyTitle = '',
    emptyHelp = '',
    agents = [],
    projects = [],
    page = 0,
    pageCount = 1,
    onOpen = noop,
    onContextMenu = noop,
    onPage = noop,
    onClearFilters = noop,
  } = $props();

  let listElement = $state();

  function openMenu(entry, event) {
    event.preventDefault();
    onContextMenu(entry, event);
  }

  export function scrollToTop() {
    listElement?.scrollTo?.(0, 0);
  }
</script>

<div class="s-group-toolbar skills-results-meta" aria-live="polite">
  <span class="s-group-toolbar__meta"
    >{total === 1
      ? t('skills.resultCountOne')
      : t('skills.resultCount', { count: total })}</span
  >
  {#if loading}<span class="s-group-toolbar__meta"
      >{t('skills.refreshing')}</span
    >{/if}
  {#if filtersActive}
    <Button variant="secondary" onClick={onClearFilters}
      >{t('skills.clearFilters')}</Button
    >
  {/if}
</div>
<div class="skills-list" bind:this={listElement}>
  {#if loading && !loaded}
    <Banner variant="neutral">{t('skills.loading')}</Banner>
  {:else if !total}
    <EmptyState
      title={filtersActive ? t('skills.noMatches') : emptyTitle}
      description={filtersActive ? t('skills.noMatchesHelp') : emptyHelp}
    />
  {:else}
    <div class="s-group skills-group">
      <div class="skills-rows">
        {#each entries as entry (entry.id)}
          <button
            type="button"
            class="skills-row"
            class:skills-row--current={currentId === entry.id}
            class:skills-row--disabled={entry.disabled}
            data-skill-id={entry.id}
            use:tooltip={() => skillRowDetails(entry, agents, projects)}
            onclick={() => onOpen(entry)}
            oncontextmenu={(event) => openMenu(entry, event)}
            onkeydown={(event) => {
              if (isContextMenuKey(event)) openMenu(entry, event);
            }}
          >
            <span class="skills-row-name">{entry.name}</span>
            {#if entry.status !== 'available'}<StatusChip
                variant={skillStatusVariant(entry)}
                >{skillStatusLabel(entry)}</StatusChip
              >
            {:else if skillDiagnosticLines(entry).length}<Badge variant="warn"
                >{t('skills.requirementNotes')}</Badge
              >{/if}
            <span class="skills-row-meta">
              <span class="skills-row-source">{skillSourceLabel(entry)}</span>
              <span class="skills-row-summary"
                >{skillAccessSummary(entry, agents, projects)}</span
              >
            </span>
          </button>
        {/each}
      </div>
    </div>
  {/if}
</div>
{#if pageCount > 1}
  <div class="skills-pagination">
    <Button
      variant="secondary"
      disabled={page === 0}
      onClick={() => onPage(page - 1)}
      ariaLabel={t('skills.previousPage')}>←</Button
    >
    <span>{t('skills.page', { page: page + 1, pages: pageCount })}</span>
    <Button
      variant="secondary"
      disabled={page + 1 === pageCount}
      onClick={() => onPage(page + 1)}
      ariaLabel={t('skills.nextPage')}>→</Button
    >
  </div>
{/if}
