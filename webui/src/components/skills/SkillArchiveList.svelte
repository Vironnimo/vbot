<script>
  // The archived packages of every editable scope, newest first, one line
  // each: the name, then, muted, where it lived and when and why it was
  // archived. Like every Skill list it never shows the description inline,
  // only in the row's tooltip (explicit user requirement, see the Skills
  // section of webui/design.md). An archived package has no page: a click,
  // a right click or the context menu key asks `onMenu(item, event)` for its
  // actions.
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
    onMenu = noop,
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
          <button
            type="button"
            class="skills-row"
            aria-haspopup="menu"
            data-archive-key={archivedSkillKey(item)}
            use:tooltip={() => archivedSkillDetails(item, agents)}
            onclick={(event) => onMenu(item, event)}
            oncontextmenu={(event) => openMenu(item, event)}
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
        {/each}
      </div>
    </div>
  {/if}
</div>
