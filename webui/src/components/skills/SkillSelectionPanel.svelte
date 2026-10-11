<script>
  // The Skills of one Agent or Project as selection groups (one
  // AgentSelectionGroup per group) under a header with the active count, an
  // optional "add new skills automatically" switch and an optional filter.
  // Callers own what toggling means; the Skills manager, the Agent editor and
  // the Project editor share this one presentation. `onContextMenu(groupId,
  // item, event)` offers a row's context menu (see AgentSelectionGroup), also
  // from a "⋯" button on every row, so a Skill's actions are found where it
  // is seen. The panel carries its own layout styles: it is also rendered
  // outside the Skills view (Agent and Project editors, Extension pages),
  // where the Skills view's stylesheet is not loaded.
  import { t } from '$lib/i18n.js';
  import AgentSelectionGroup, {
    filterSelectionItems,
  } from '../agents/AgentSelectionGroup.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';

  const noop = () => {};
  const uid = $props.id();

  let {
    groups = [],
    active = 0,
    total = 0,
    query = '',
    showFilter = false,
    onQuery = noop,
    autoAdd = null,
    onToggle = noop,
    onSetAll = noop,
    onOpen = null,
    onContextMenu = null,
    columns = false,
    emptyTitle = '',
    emptyHelp = '',
    class: className = '',
  } = $props();

  let menuGroups = $derived(
    onContextMenu
      ? groups.map((group) => ({
          ...group,
          items: group.items.map((item) => ({
            ...item,
            menuButton: {
              ariaLabel: t('skills.menu.label', { name: item.name }),
              tooltip: t('skills.menu.more'),
            },
          })),
        }))
      : groups,
  );

  let visibleGroups = $derived(
    query.trim()
      ? menuGroups.filter(
          (group) => filterSelectionItems(group.items, query).length > 0,
        )
      : menuGroups,
  );
</script>

<div class={['skills-selection', className].filter(Boolean).join(' ')}>
  {#if total > 0 || autoAdd}
    <div class="s-group-toolbar">
      <span class="s-group-toolbar__meta" aria-live="polite"
        >{t('skills.panel.count', { active, total })}</span
      >
      {#if autoAdd}
        <label class="skills-selection__auto">
          <Toggle
            size="sm"
            checked={autoAdd.checked}
            ariaLabel={t('skills.panel.autoAdd')}
            onChange={autoAdd.onChange}
          />
          <span>{t('skills.panel.autoAdd')}</span>
        </label>
        <InfoHint text={t('skills.panel.autoAddHelp')} />
      {/if}
      {#if showFilter && total > 1}
        <TextField
          type="search"
          value={query}
          placeholder={t('skills.panel.filterPlaceholder')}
          ariaLabel={t('skills.panel.filter')}
          onInput={onQuery}
        />
      {/if}
    </div>
  {/if}
  {#if total === 0}
    <EmptyState density="compact" title={emptyTitle} description={emptyHelp} />
  {:else if visibleGroups.length === 0}
    <EmptyState density="compact" title={t('skills.noMatches')} />
  {:else}
    <div class={columns ? 's-check-groups' : 'skills-selection__groups'}>
      {#each visibleGroups as group (group.id)}
        <AgentSelectionGroup
          title={group.title}
          titleId={`${uid}-${group.id}`}
          items={group.items}
          {query}
          allLabel={group.allLabel}
          toggleLabel={(name) => t('skills.panel.toggle', { name })}
          onToggle={(name, next, item) => onToggle(group.id, name, next, item)}
          onSetAll={(next) => onSetAll(group.id, next)}
          {onOpen}
          onContextMenu={onContextMenu &&
            ((item, event) => onContextMenu(group.id, item, event))}
        />
      {/each}
    </div>
  {/if}
</div>

<style>
  .skills-selection {
    display: grid;
    gap: 12px;
    min-width: 0;
  }

  .skills-selection__auto {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-med);
    font: 500 var(--fs-body-sm) / 1.4 var(--font-ui);
    cursor: pointer;
  }

  .skills-selection__groups {
    display: grid;
    gap: 12px;
  }
</style>
