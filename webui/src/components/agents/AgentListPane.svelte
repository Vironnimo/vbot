<script>
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
    onCopyId = () => {},
    onDelete = () => {},
  } = $props();

  // The open row context menu (../ui/ContextMenu.svelte), or null.
  let menu = $state(null);

  // Open chat, Copy ID | Delete... (the last Agent cannot be deleted).
  function agentMenu(agent) {
    const lastAgent = agents.length < 2;
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
          id: 'copy-id',
          label: t('agents.menu.copyId'),
          group: 'agent',
          onSelect: () => onCopyId(agent.id),
        },
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

  function reorderAgents(from, to) {
    return onReorder(moveItem(agents, from, to).map((agent) => agent.id));
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
        items={agents}
        getLabel={(agent) => agent.name || agent.id}
        disabled={isReordering}
        aria-label={t('agents.title')}
        onReorder={reorderAgents}
        onDragActiveChange={onReorderInteractionChange}
      >
        {#snippet item(agent)}
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
              <div class="agent-item-name">{agent.name || agent.id}</div>
              <div class="agent-item-sub">
                {modelShortName(agent.model) || agent.id || t('common.unknown')}
              </div>
            </div>
          </button>
        {/snippet}
      </SortableList>
    {/if}
  </div>
</aside>

<ContextMenu {menu} onClose={() => (menu = null)} />
