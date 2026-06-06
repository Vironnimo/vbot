<script>
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
    <button class="btn-new" type="button" onclick={onCreate}>
      <svg viewBox="0 0 14 14" aria-hidden="true">
        <path d="M7 1v12M1 7h12" />
      </svg>
      {t('common.new', 'New')}
    </button>
  </div>

  <div class="agent-list-scroll">
    {#if isLoading}
      <p class="agents-view__list-state">
        {t('agents.loading', 'Loading agents…')}
      </p>
    {:else if agents.length === 0}
      <div class="empty-state agents-view__empty-list">
        <svg class="empty-state-icon" viewBox="0 0 32 32" aria-hidden="true">
          <circle cx="16" cy="10" r="5" />
          <path d="M6 28c0-5.5 4.5-10 10-10s10 4.5 10 10" />
        </svg>
        <div class="empty-state-title">
          {t('agents.empty', 'No agents found.')}
        </div>
        <div class="empty-state-sub">
          {t(
            'agents.emptyCreateHint',
            'Create an agent to begin configuring chat access.',
          )}
        </div>
      </div>
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
