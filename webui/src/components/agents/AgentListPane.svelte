<script>
  import { tick } from 'svelte';

  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import { t } from '$lib/i18n.js';
  import { modelShortName } from '$lib/modelSelection.js';

  let {
    agents = [],
    selectedAgentId = '',
    isLoading = false,
    isReordering = false,
    onSelect = () => {},
    onCreate = () => {},
    onReorder = async () => {},
    onReorderInteractionChange = () => {},
  } = $props();

  let dragSourceIndex = $state(null);
  let dragTargetIndex = $state(null);
  let reorderAnnouncement = $state('');

  function handleDragStart(index, event) {
    if (isReordering || agents.length < 2) {
      event.preventDefault();
      return;
    }
    dragSourceIndex = index;
    dragTargetIndex = index;
    onReorderInteractionChange(true);
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', agents[index].id);
    }
  }

  function handleDragOver(index, event) {
    if (dragSourceIndex === null || isReordering) {
      return;
    }
    event.preventDefault();
    dragTargetIndex = index;
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'move';
    }
  }

  function handleDrop(index, event) {
    event.preventDefault();
    const sourceIndex = dragSourceIndex;
    clearDragState();
    if (sourceIndex === null || sourceIndex === index) {
      return;
    }
    void moveAgent(sourceIndex, index);
  }

  function handleDragEnd() {
    clearDragState();
  }

  function clearDragState() {
    dragSourceIndex = null;
    dragTargetIndex = null;
    onReorderInteractionChange(false);
  }

  function handleHandleKeydown(index, event) {
    let targetIndex;
    if (event.key === 'ArrowUp') {
      targetIndex = index - 1;
    } else if (event.key === 'ArrowDown') {
      targetIndex = index + 1;
    } else {
      return;
    }

    event.preventDefault();
    if (isReordering || targetIndex < 0 || targetIndex >= agents.length) {
      return;
    }
    void moveAgent(index, targetIndex);
  }

  async function moveAgent(sourceIndex, targetIndex) {
    const nextAgents = [...agents];
    const [movedAgent] = nextAgents.splice(sourceIndex, 1);
    nextAgents.splice(targetIndex, 0, movedAgent);
    const persistence = onReorder(nextAgents.map((agent) => agent.id));
    reorderAnnouncement = t(
      'agents.order.announcement',
      'Moved {name} to position {position} of {total}',
      {
        name: movedAgent.name || movedAgent.id,
        position: targetIndex + 1,
        total: nextAgents.length,
      },
    );
    await persistence;
    await tick();
    document
      .querySelector(`[data-agent-order-handle="${movedAgent.id}"]`)
      ?.focus();
  }
</script>

<aside
  class="agent-list-pane secondary-pane"
  aria-labelledby="agents-list-title"
>
  <div class="pane-header secondary-pane__header">
    <span id="agents-list-title" class="secondary-pane__title">
      {t('agents.title', 'Agents')}
    </span>
    <Button variant="primary" onClick={onCreate}>
      <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
        <path d="M7 1v12M1 7h12" />
      </svg>
      {t('common.add', 'Add')}
    </Button>
  </div>

  <div
    class="agent-list-scroll secondary-pane__scroll secondary-list"
    role={!isLoading && agents.length > 0 ? 'list' : undefined}
  >
    {#if isLoading}
      <p class="agents-view__list-state">
        {t('agents.loading', 'Loading agents…')}
      </p>
    {:else if agents.length === 0}
      <EmptyState
        class="agent-list-pane__empty"
        title={t('agents.empty', 'No agents found.')}
        description={t(
          'agents.emptyCreateHint',
          'Create an agent to begin configuring chat access.',
        )}
      >
        {#snippet icon()}
          <svg viewBox="0 0 32 32" width="34" height="34">
            <circle cx="16" cy="10" r="5" />
            <path d="M6 28c0-5.5 4.5-10 10-10s10 4.5 10 10" />
          </svg>
        {/snippet}
      </EmptyState>
    {:else}
      {#each agents as agent, index (agent.id)}
        <div
          class="agent-list-row"
          role="listitem"
          class:agent-list-row--drop-target={dragTargetIndex === index &&
            dragSourceIndex !== index}
          ondragover={(event) => handleDragOver(index, event)}
          ondrop={(event) => handleDrop(index, event)}
        >
          <button
            class:active={agent.id === selectedAgentId}
            class="agent-item secondary-list__item"
            type="button"
            onclick={() => onSelect(agent.id)}
          >
            <div class="agent-item-inner">
              <div class="agent-item-name">{agent.name || agent.id}</div>
              <div class="agent-item-sub">
                {modelShortName(agent.model) ||
                  agent.id ||
                  t('common.unknown', 'Unknown')}
              </div>
            </div>
          </button>
          <button
            type="button"
            class="agent-order-handle"
            draggable={!isReordering && agents.length > 1}
            disabled={isReordering || agents.length < 2}
            data-agent-order-handle={agent.id}
            aria-label={t(
              'agents.order.handle',
              'Reorder {name} (use arrow keys)',
              { name: agent.name || agent.id },
            )}
            ondragstart={(event) => handleDragStart(index, event)}
            ondragend={handleDragEnd}
            onkeydown={(event) => handleHandleKeydown(index, event)}
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              aria-hidden="true"
              focusable="false"
            >
              <circle cx="3.5" cy="2.5" r="1.1" fill="currentColor" />
              <circle cx="8.5" cy="2.5" r="1.1" fill="currentColor" />
              <circle cx="3.5" cy="6" r="1.1" fill="currentColor" />
              <circle cx="8.5" cy="6" r="1.1" fill="currentColor" />
              <circle cx="3.5" cy="9.5" r="1.1" fill="currentColor" />
              <circle cx="8.5" cy="9.5" r="1.1" fill="currentColor" />
            </svg>
          </button>
        </div>
      {/each}
    {/if}
  </div>
  <div class="agent-list-pane__sr-only" aria-live="polite" role="status">
    {reorderAnnouncement}
  </div>
</aside>
