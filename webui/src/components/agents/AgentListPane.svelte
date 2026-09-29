<script>
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import SortableList from '../ui/SortableList.svelte';
  import { t } from '$lib/i18n.js';
  import { modelShortName } from '$lib/modelSelection.js';
  import { moveItem } from '../ui/sortable.js';

  let {
    agents = [],
    selectedAgentId = '',

    sharedDefaultsOpen = false,

    onOpenSharedDefaults = () => {},

    isLoading = false,
    isReordering = false,
    onSelect = () => {},
    onCreate = () => {},
    onReorder = async () => {},
    onReorderInteractionChange = () => {},
  } = $props();

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

  <div class="agent-list-defaults">
    <Button
      variant="tertiary"
      class={`secondary-list__item ${sharedDefaultsOpen ? 'active' : ''}`}
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
            onclick={() => onSelect(agent.id)}
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
