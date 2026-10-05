<script>
  // The archived packages of every editable scope, newest first, one line
  // each: the name, then, muted, where it lived and when and why it was
  // archived. Like every Skill list it never shows the description inline,
  // only in the row's tooltip (explicit user requirement, see the Skills
  // section of webui/design.md). Restore and Delete permanently stay visible
  // at the end of each row. An archived package has no page: a click on the
  // name part, a right click or the context menu key asks
  // `onMenu(item, event)` for its menu.
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import { isContextMenuKey } from '../ui/contextMenu.js';
  import {
    archivedSkillDetails,
    archivedSkillKey,
    archivedSkillSummary,
    skillScopeLabel,
  } from './skillRecords.js';

  const noop = () => {};

  let {
    items = [],
    loading = false,
    loaded = false,
    filtersActive = false,
    emptyTitle = '',
    emptyHelp = '',
    agents = [],
    busy = false,
    onMenu = noop,
    onRestore = noop,
    onPurge = noop,
    onClearFilters = noop,
  } = $props();

  function openMenu(item, event) {
    event.preventDefault();
    onMenu(item, event);
  }
</script>

<div class="s-group-toolbar skills-results-meta" aria-live="polite">
  <span class="s-group-toolbar__meta"
    >{items.length === 1
      ? t('skills.resultCountOne')
      : t('skills.resultCount', { count: items.length })}</span
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
<div class="skills-list">
  {#if loading && !loaded}
    <Banner variant="neutral">{t('skills.loading')}</Banner>
  {:else if !items.length}
    <EmptyState
      title={filtersActive ? t('skills.noMatches') : emptyTitle}
      description={filtersActive ? t('skills.noMatchesHelp') : emptyHelp}
    />
  {:else}
    <div class="s-group skills-group">
      <div class="skills-rows">
        {#each items as item (archivedSkillKey(item))}
          <!-- svelte-ignore a11y_no_static_element_interactions (Right click anywhere on the row, including its buttons; the context menu key on the row's open button is the keyboard path.) -->
          <div
            class="skills-row"
            oncontextmenu={(event) => openMenu(item, event)}
          >
            <button
              type="button"
              class="skills-row-open"
              data-archive-key={archivedSkillKey(item)}
              aria-haspopup="menu"
              use:tooltip={() => archivedSkillDetails(item, agents)}
              onclick={(event) => onMenu(item, event)}
              onkeydown={(event) => {
                if (isContextMenuKey(event)) openMenu(item, event);
              }}
            >
              <span class="skills-row-name">{item.name}</span>
              <span class="skills-row-meta">
                <span class="skills-row-source"
                  >{skillScopeLabel(item.scope)}</span
                >
                <span class="skills-row-summary"
                  >{archivedSkillSummary(item)}</span
                >
              </span>
            </button>
            <span class="skills-row-actions">
              <Button
                variant="tertiary"
                disabled={busy}
                ariaLabel={t('skills.row.restore', { name: item.name })}
                onClick={() => onRestore(item)}
                >{t('skills.archived.restore')}</Button
              >
              <Button
                variant="tertiary"
                icon
                class="skills-row-delete"
                disabled={busy}
                ariaLabel={t('skills.row.purge', { name: item.name })}
                tooltip={t('skills.archived.purgeTitle')}
                onClick={() => onPurge(item)}
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
