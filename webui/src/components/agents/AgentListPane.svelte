<script>
  import Button from '../ui/Button.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import { t } from '$lib/i18n.js';

  let {
    agents = [],
    selectedAgentId = '',
    isLoading = false,
    onSelect = () => {},
    onCreate = () => {},
  } = $props();
</script>

<aside class="agent-list-pane" aria-labelledby="agents-list-title">
  <div class="pane-header">
    <span id="agents-list-title" class="pane-title">
      {t('agents.title', 'Agents')}
    </span>
    <Button variant="primary" onClick={onCreate}>
      <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
        <path d="M7 1v12M1 7h12" />
      </svg>
      {t('common.add', 'Add')}
    </Button>
  </div>

  <div class="agent-list-scroll">
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
      {#each agents as agent (agent.id)}
        <button
          class:active={agent.id === selectedAgentId}
          class="agent-item"
          type="button"
          onclick={() => onSelect(agent.id)}
        >
          <div class="agent-bar"></div>
          <div class="agent-item-inner">
            <div class="agent-item-name">{agent.name || agent.id}</div>
            <div class="agent-item-sub">
              {agent.model || agent.id || t('common.unknown', 'Unknown')}
            </div>
          </div>
        </button>
      {/each}
    {/if}
  </div>
</aside>
