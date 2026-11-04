<script>
  import { untrack } from 'svelte';
  import {
    agentPlacement,
    isChatAgent,
    privateAgents,
    helperAgents,
    reorderAgentGroup,
    canDeleteAgent,
  } from '$lib/agentPlacement.js';
  import StatusChip from '../ui/StatusChip.svelte';
  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from '../ui/contextMenu.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import SortableList from '../ui/SortableList.svelte';
  import { t } from '$lib/i18n.js';
  import { modelSelectionParts, modelShortName } from '$lib/modelSelection.js';
  import { tooltip } from '$lib/tooltip.js';
  import { moveItem } from '../ui/sortable.js';

  let {
    agents = [],
    selectedAgentId = '',

    sharedDefaultsOpen = false,

    onOpenSharedDefaults = () => {},

    isLoading = false,
    isReordering = false,
    // The Agent being deleted; no other deletion starts meanwhile.
    deletingAgentId = '',
    onSelect = () => {},
    onCreate = () => {},
    onReorder = async () => {},
    onReorderInteractionChange = () => {},
    // Row context menu actions.
    onOpenChat = () => {},
    onOpenInSplit = () => {},
    onCopyId = () => {},
    onDelete = () => {},
  } = $props();

  // The open row context menu (../ui/ContextMenu.svelte), or null.
  let menu = $state(null);

  // Open chat, Open in split view, Copy ID | Delete... (the last Agent
  // cannot be deleted).
  function agentMenu(agent) {
    const lastAgent = !canDeleteAgent(agent, agents);
    return {
      label: t('agents.menu.label', { name: agent.name || agent.id }),
      items: [
        {
          id: 'open-chat',
          label: t('agents.menu.openChat'),
          group: 'agent',
          onSelect: () => onOpenChat(agent.id),
        },
        {
          id: 'open-in-split',
          label: t('split.openInSplit'),
          group: 'agent',
          onSelect: () => onOpenInSplit(agent.id),
        },
        {
          id: 'copy-id',
          label: t('agents.menu.copyId'),
          group: 'agent',
          onSelect: () => onCopyId(agent.id),
        },
        ...(isChatAgent(agent)
          ? [
              {
                id: 'new-private',
                label: t('agents.list.newPrivateAgent'),
                group: 'agent',
                onSelect: () =>
                  onCreate({ kind: 'private', owner_id: agent.id }),
              },
            ]
          : []),
        {
          id: 'delete',
          label: t('agents.menu.delete'),
          danger: true,
          group: 'delete',
          disabled: lastAgent || Boolean(deletingAgentId),
          hint: lastAgent ? t('agents.menu.lastAgent') : undefined,
          onSelect: () => onDelete(agent),
        },
      ],
    };
  }

  // A touch hold that already started a reorder drag prevents the
  // `contextmenu` that follows it, so the drag keeps the gesture.
  function openMenu(agent, event) {
    if (event.defaultPrevented) return;
    event.preventDefault();
    menu = { ...contextMenuAnchor(event), ...agentMenu(agent) };
  }

  // The row's details card: the full Model (the row shows its short name)
  // and whether it is inherited, then the id when a name leads.
  function agentDetails(agent) {
    const { model } = modelSelectionParts(agent.model);
    const inherited = agent.effective?.model?.source === 'global_default';
    return {
      title: agent.name || agent.id,
      text: inherited ? t('agents.details.modelInherited') : '',
      rows: [
        { label: t('agents.placement.title'), value: placementText(agent) },
        {
          label: t('agents.form.model'),
          value: model || t('agents.details.modelNotConfigured'),
          tone: model ? undefined : 'muted',
        },
        {
          label: t('agents.details.id'),
          value: agent.name ? agent.id : '',
          mono: true,
        },
      ],
      placement: 'right',
      alignTo: '.agent-item-name',
    };
  }

  let chatAgents = $derived(agents.filter(isChatAgent));
  let helpers = $derived(helperAgents(agents));
  let expansion = $state(readExpansion());
  function readExpansion() {
    try {
      return JSON.parse(
        localStorage.getItem('vbot.agentPlacement.expansion') || '{}',
      );
    } catch {
      return {};
    }
  }
  function toggleGroup(id) {
    expansion = { ...expansion, [id]: !expansion[id] };
    try {
      localStorage.setItem(
        'vbot.agentPlacement.expansion',
        JSON.stringify(expansion),
      );
    } catch {
      /* Storage can be unavailable. */
    }
  }
  $effect(() => {
    const selected = agents.find((agent) => agent.id === selectedAgentId);
    if (!selected) return;
    const placement = agentPlacement(selected);
    const group =
      placement.kind === 'private' && !placement.orphaned
        ? placement.owner_id
        : !isChatAgent(selected)
          ? '~helpers'
          : '';
    if (group)
      untrack(() => {
        if (!expansion[group]) expansion = { ...expansion, [group]: true };
      });
  });
  function placementText(agent) {
    const placement = agentPlacement(agent);
    if (placement.orphaned) return t('agents.placement.orphaned');
    if (placement.kind === 'private')
      return t('agents.placement.privateTo', {
        owner:
          agents.find((owner) => owner.id === placement.owner_id)?.name ||
          placement.owner_id,
      });
    return isChatAgent(agent)
      ? t('agents.placement.chat')
      : t('agents.placement.helper');
  }
  function reorderGroup(group, from, to) {
    return onReorder(
      reorderAgentGroup(
        agents,
        moveItem(group, from, to).map((agent) => agent.id),
      ),
    );
  }
</script>

<aside
  class="agent-list-pane secondary-pane"
  aria-labelledby="agents-list-title"
>
  <div class="pane-header secondary-pane__header">
    <span id="agents-list-title" class="secondary-pane__title">
      {t('agents.title')}
    </span>
    <Button
      variant="tertiary"
      icon
      ariaLabel={t('agents.create')}
      tooltip={t('agents.create')}
      onClick={onCreate}
    >
      <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
        <path d="M7 1.5v11M1.5 7h11" />
      </svg>
    </Button>
  </div>

  <div class="agent-list-defaults secondary-pane__pinned">
    <Button
      variant="tertiary"
      class={`secondary-list__item secondary-pane__pinned-item ${sharedDefaultsOpen ? 'active' : ''}`}
      aria-pressed={sharedDefaultsOpen}
      onClick={onOpenSharedDefaults}>{t('agents.shared.title')}</Button
    >
  </div>

  <div class="agent-list-scroll secondary-pane__scroll secondary-list">
    {#if isLoading}
      <p class="agents-view__list-state">
        {t('agents.loading')}
      </p>
    {:else if agents.length === 0}
      <EmptyState
        class="agent-list-pane__empty"
        title={t('agents.empty')}
        description={t('agents.emptyCreateHint')}
      >
        {#snippet icon()}
          <svg viewBox="0 0 32 32" width="34" height="34">
            <circle cx="16" cy="10" r="5" />
            <path d="M6 28c0-5.5 4.5-10 10-10s10 4.5 10 10" />
          </svg>
        {/snippet}
      </EmptyState>
    {:else}
      <SortableList
        class="agent-list"
        itemClass="agent-list-row"
        items={chatAgents}
        getLabel={(agent) => agent.name || agent.id}
        disabled={isReordering}
        aria-label={t('agents.title')}
        onReorder={(from, to) => reorderGroup(chatAgents, from, to)}
        onDragActiveChange={onReorderInteractionChange}
      >
        {#snippet item(agent)}
          {@const children = privateAgents(agents, agent.id)}
          <div
            class="agent-placement-row"
            class:agent-placement-row--expanded={expansion[agent.id]}
          >
            {@render agentRow(agent, children.length)}
            {#if children.length}
              <button
                class="agent-placement-toggle"
                type="button"
                data-sortable-ignore
                aria-expanded={Boolean(expansion[agent.id])}
                aria-controls={`agent-private-${agent.id}`}
                aria-label={t('agents.placement.privateTo', {
                  owner: agent.name || agent.id,
                })}
                use:tooltip={t('agents.list.privateCount', {
                  count: children.length,
                })}
                onclick={() => toggleGroup(agent.id)}
              >
                <svg
                  viewBox="0 0 16 16"
                  width="12"
                  height="12"
                  aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg
                >
              </button>
            {/if}
          </div>
          {#if children.length && expansion[agent.id]}
            <div
              class="agent-placement-children"
              id={`agent-private-${agent.id}`}
            >
              {@render groupList(children, agent.id)}
              <div class="agent-placement-actions">
                <button
                  type="button"
                  data-sortable-ignore
                  onclick={() =>
                    onCreate({ kind: 'private', owner_id: agent.id })}
                  >{t('agents.list.newPrivateAgent')}</button
                >
              </div>
            </div>
          {/if}
        {/snippet}
      </SortableList>
      {#if helpers.length}
        <div class="agent-placement-helpers">
          <button
            type="button"
            class="agent-placement-helper-toggle"
            aria-expanded={Boolean(expansion['~helpers'])}
            aria-controls="agent-helper-list"
            onclick={() => toggleGroup('~helpers')}
          >
            <svg
              class="agent-placement-chevron"
              viewBox="0 0 16 16"
              aria-hidden="true"
              ><path
                d="m6 4 4 4-4 4"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              /></svg
            >
            <span class="agent-placement-group-name"
              >{t('agents.list.helperAgents')}</span
            ><span class="agent-placement-group-count">{helpers.length}</span>
          </button>
          <button
            class="agent-placement-new"
            type="button"
            aria-label={t('agents.create')}
            use:tooltip={t('agents.create')}
            onclick={() => onCreate({ kind: 'helper' })}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true"
              ><path
                d="M8 3.5v9M3.5 8h9"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
              /></svg
            >
          </button>
        </div>
        {#if expansion['~helpers']}<div id="agent-helper-list">
            {@render groupList(helpers, '~helpers')}
          </div>{/if}
      {/if}
    {/if}
  </div>
</aside>

<ContextMenu {menu} onClose={() => (menu = null)} />

{#snippet agentRow(agent, privateCount = 0)}
  <button
    class:active={agent.id === selectedAgentId}
    class="agent-item secondary-list__item"
    type="button"
    use:tooltip={() => agentDetails(agent)}
    onclick={() => onSelect(agent.id)}
    oncontextmenu={(event) => openMenu(agent, event)}
    onkeydown={(event) => {
      if (isContextMenuKey(event)) openMenu(agent, event);
    }}
  >
    <div class="agent-item-inner">
      <div class="agent-item-name">
        <span>{agent.name || agent.id}</span>{#if privateCount}<span
            class="agent-placement-count"
            aria-label={t('agents.list.privateCount', { count: privateCount })}
            >{privateCount}</span
          >{/if}
      </div>
      <div class="agent-item-sub">
        {modelShortName(agent.model) || agent.id || t('common.unknown')}
      </div>
    </div>
    {#if agentPlacement(agent).orphaned}<StatusChip variant="warn"
        >{t('agents.placement.orphaned')}</StatusChip
      >{/if}
  </button>
{/snippet}
{#snippet groupList(group, id)}
  <SortableList
    class="agent-list"
    itemClass="agent-list-row"
    items={group}
    getLabel={(agent) => agent.name || agent.id}
    disabled={isReordering}
    aria-label={id === '~helpers'
      ? t('agents.list.helperAgents')
      : t('agents.list.privateCount', { count: group.length })}
    onReorder={(from, to) => reorderGroup(group, from, to)}
    onDragActiveChange={onReorderInteractionChange}
  >
    {#snippet item(agent)}{@render agentRow(agent)}{/snippet}
  </SortableList>
{/snippet}

<style>
  /* Same dense, full-width rows and trailing disclosure as Schedules. */
  .agent-list-scroll {
    padding: 6px;
  }
  .agent-list-pane .agent-item-inner {
    padding: 4px 10px;
  }
  .agent-list-pane .agent-item-sub {
    margin-top: 1px;
  }
  .agent-item-name {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .agent-item-name > span:first-child {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .agent-placement-row {
    position: relative;
  }
  .agent-placement-row:has(.agent-placement-toggle) .agent-item-name {
    padding-right: 20px;
  }
  .agent-placement-toggle {
    position: absolute;
    top: 2px;
    right: 4px;
    display: grid;
    width: 20px;
    height: 20px;
    place-items: center;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-lo);
    cursor: pointer;
    opacity: 0;
    transition:
      background 120ms ease,
      color 120ms ease,
      opacity 120ms ease;
  }
  .agent-placement-row:is(:hover, :focus-within, .agent-placement-row--expanded)
    .agent-placement-toggle {
    opacity: 1;
  }
  .agent-placement-toggle:hover,
  .agent-placement-toggle:focus-visible {
    background: var(--surface-3);
    color: var(--text-hi);
  }
  .agent-placement-toggle:focus-visible {
    box-shadow: inset 0 0 0 2px var(--accent);
    outline: none;
  }
  .agent-placement-toggle svg {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.5;
    transition: transform 120ms ease;
  }
  .agent-placement-toggle[aria-expanded='true'] svg {
    transform: rotate(90deg);
  }
  .agent-placement-children {
    padding: 0 0 4px 14px;
  }
  .agent-placement-count {
    flex-shrink: 0;
    margin-left: auto;
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }
  .agent-placement-actions {
    padding: 3px 10px 2px;
  }
  .agent-placement-actions button {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--text-med);
    font: inherit;
    font-size: var(--fs-label-sm);
    cursor: pointer;
  }
  .agent-placement-actions button:hover,
  .agent-placement-actions button:focus-visible {
    color: var(--text-hi);
  }
  /* The Helper header follows the Session list's compact Project headers. */
  .agent-placement-helpers {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 0 4px;
    margin-top: 8px;
  }
  .agent-placement-helper-toggle {
    display: flex;
    align-items: center;
    min-width: 0;
    flex: 1;
    gap: 4px;
    height: 26px;
    padding: 0 4px;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    font: 600 var(--fs-label-sm) var(--font-ui);
    text-align: left;
    cursor: pointer;
  }
  .agent-placement-helper-toggle:hover,
  .agent-placement-helper-toggle:focus-visible {
    color: var(--text-hi);
  }
  .agent-placement-helper-toggle:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: -1px;
  }
  .agent-placement-chevron {
    width: 12px;
    height: 12px;
    flex-shrink: 0;
    transition: transform 150ms ease;
  }
  .agent-placement-helper-toggle[aria-expanded='true']
    .agent-placement-chevron {
    transform: rotate(90deg);
  }
  .agent-placement-group-name {
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .agent-placement-group-count {
    color: var(--text-lo);
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }
  .agent-placement-new {
    display: grid;
    place-items: center;
    width: 24px;
    height: 24px;
    padding: 0;
    flex-shrink: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    cursor: pointer;
  }
  .agent-placement-new svg {
    width: 16px;
    height: 16px;
  }
  .agent-placement-new:hover,
  .agent-placement-new:focus-visible {
    color: var(--text-hi);
    background: var(--surface-2);
  }
  .agent-placement-new:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: -1px;
  }
  @media (hover: none) {
    .agent-placement-toggle {
      opacity: 1;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .agent-placement-toggle,
    .agent-placement-toggle svg,
    .agent-placement-chevron {
      transition: none;
    }
  }
</style>
