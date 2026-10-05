<script>
  // One page of Skill packages, one line each: the name with problem badges,
  // a quiet source label with who gets the package, then the package's own
  // controls, always visible: an on/off switch, Edit and Delete. A read-only
  // package keeps Edit and Delete disabled with the reason as their tooltip.
  // The description is never shown inline, only in the tooltip of the row's
  // name part (explicit user requirement, see the Skills section of
  // webui/design.md). The name part opens the package's page; a right click
  // or the context menu key asks `onContextMenu(entry, event)` for its menu
  // (default already prevented).
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import { skillAccessSummary, skillRowDetails } from './skillAccess.js';
  import {
    skillDiagnosticLines,
    skillReadOnlyReason,
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
    busy = false,
    onOpen = noop,
    onEdit = noop,
    onDelete = noop,
    onSetDisabled = noop,
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
          {@const readOnlyReason = entry.editable_scope
            ? ''
            : skillReadOnlyReason(entry)}
          <div
            class="skills-row"
            class:skills-row--current={currentId === entry.id}
            class:skills-row--disabled={entry.disabled}
            oncontextmenu={(event) => openMenu(entry, event)}
          >
            <button
              type="button"
              class="skills-row-open"
              data-skill-id={entry.id}
              use:tooltip={() => skillRowDetails(entry, agents, projects)}
              onclick={() => onOpen(entry)}
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
            <span class="skills-row-actions">
              <span
                class="tooltip-anchor skills-row-switch"
                use:tooltip={entry.disabled
                  ? t('skills.row.offHint')
                  : t('skills.row.onHint')}
                ><Toggle
                  size="sm"
                  checked={!entry.disabled}
                  disabled={busy}
                  ariaLabel={t('skills.row.toggle', { name: entry.name })}
                  onChange={(on) => onSetDisabled(entry, !on)}
                /></span
              >
              <Button
                variant="tertiary"
                icon
                disabled={!entry.editable_scope || busy}
                disabledReason={readOnlyReason}
                ariaLabel={t('skills.row.edit', { name: entry.name })}
                tooltip={t('skills.editInstructions')}
                onClick={() => onEdit(entry)}
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.5"
                  stroke-linejoin="round"
                  aria-hidden="true"
                  ><path
                    d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z"
                  /></svg
                >
              </Button>
              <Button
                variant="tertiary"
                icon
                class="skills-row-delete"
                disabled={!entry.editable_scope || busy}
                disabledReason={readOnlyReason}
                ariaLabel={t('skills.deleteNamed', { name: entry.name })}
                tooltip={t('common.delete')}
                onClick={() => onDelete(entry)}
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 16 16"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.2"
                  stroke-linejoin="round"
                  aria-hidden="true"
                  ><path
                    d="M2 4h12M6 4V2h4v2M4 4l1 10h6l1-10M7 6v6M9 6v6"
                  /></svg
                >
              </Button>
            </span>
          </div>
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
