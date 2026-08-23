<script>
  import { t } from '$lib/i18n.js';
  import { parseModelSelectionValue } from '$lib/modelSelection.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from '../Dropdown.svelte';

  let {
    agents = [],
    agentStatuses = {},
    selectedAgentId = '',
    loadingAgents = false,
    // Project context for the compact project picker that lives in the header
    // (left of the Sessions button). "No project" is Personal/identity chat.
    projects = [],
    selectedProjectId = '',
    onSelectProject = () => {},
    onSelectAgent = () => {},
  } = $props();

  // The identity bar carries a "Personal" label only while a project is
  // selected, so it visually pairs with the project-name label on the second
  // (team) bar below. With no project there is just one bar and no label needed.
  let showPersonalLabel = $derived(
    typeof selectedProjectId === 'string' &&
      selectedProjectId.trim().length > 0,
  );
  // "No project" (Personal) plus one option per project, mirroring the chosen
  // project's display name back into the trigger label.
  let projectOptions = $derived([
    { value: '', label: t('chat.project.none', 'No project selected') },
    ...projects.map((project) => ({
      value: project.project_id,
      label: project.display_name || project.project_id,
    })),
  ]);

  function agentActivityLabel(name, status) {
    if (status === 'running') {
      return t('chat.agentActivity.running', '{name}: Running', { name });
    }
    if (status === 'unread') {
      return t('chat.agentActivity.unread', '{name}: Unread result', { name });
    }
    return t('chat.agentActivity.idle', '{name}: Idle', { name });
  }

  function agentActivityTooltip(activityLabel, modelValue) {
    const { model } = parseModelSelectionValue(
      typeof modelValue === 'string' ? modelValue.trim() : '',
    );
    return model ? `${activityLabel}\n${model}` : activityLabel;
  }
</script>

<header class="chat-header">
  <h2 id="chat-title" class="chat-title">{t('chat.title', 'Chat')}</h2>
  <div class="agent-tabs" aria-label={t('chat.selectAgent', 'Select agent')}>
    {#if showPersonalLabel}
      <span
        class="agent-tabs__personal-label"
        use:tooltip={t(
          'chat.personalBarHint',
          'Your personal agents — available with or without a project.',
        )}
      >
        {t('chat.personalBarLabel', 'Personal')}
      </span>
    {/if}
    {#if agents.length > 0}
      {#each agents as agent (agent.id)}
        {@const agentStatus = agentStatuses[agent.id] ?? 'idle'}
        {@const activityLabel = agentActivityLabel(agent.name, agentStatus)}
        {@const activityTooltip = agentActivityTooltip(
          activityLabel,
          agent.model,
        )}
        <button
          type="button"
          class:active={agent.id === selectedAgentId}
          class="agent-tab"
          disabled={loadingAgents}
          aria-label={activityLabel}
          use:tooltip={activityTooltip}
          onclick={() => onSelectAgent(agent.id)}
        >
          <span
            class="tab-indicator tab-indicator--{agentStatus}"
            aria-hidden="true"
          ></span>
          <span>{agent.name}</span>
        </button>
      {/each}
    {:else}
      <span class="agent-tab agent-tab--empty">
        <span class="tab-indicator"></span>
        {t('chat.noAgents', 'No agents are available yet.')}
      </span>
    {/if}
  </div>
  <div class="header-right">
    <Dropdown
      value={selectedProjectId}
      options={projectOptions}
      ariaLabel={t('chat.project.selectAria', 'Select project')}
      triggerClass="chat-header__project-dropdown"
      onValueChange={(next) => onSelectProject(next)}
    />
  </div>
</header>

<style>
  .chat-header {
    display: flex;
    height: 50px;
    flex-shrink: 0;
    align-items: center;
    gap: 8px;
    padding: 0 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-title {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }

  .agent-tabs {
    display: flex;
    min-width: 0;
    height: 100%;
    flex: 1;
    align-items: stretch;
    gap: 2px;
    overflow-x: auto;
  }

  .agent-tab {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 7px;
    padding: 0 14px;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--text-lo);
    background: transparent;
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    font-weight: 500;
    white-space: nowrap;
    transition:
      border-color 150ms ease,
      color 150ms ease;
  }

  .agent-tab:hover,
  .agent-tab:focus-visible {
    color: var(--text-med);
    outline: none;
  }

  .agent-tab.active {
    border-bottom-color: var(--accent);
    color: var(--accent);
  }

  .agent-tab--empty {
    cursor: default;
  }

  /* Bold "Personal" label before the identity agent tabs, mirroring the
     project-name label on the team bar below (.chat-view__project-team-name)
     so the two bars read as a matched pair when a project is selected. */
  .agent-tabs__personal-label {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    margin-right: 6px;
    padding-right: 12px;
    border-right: 1px solid var(--border);
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    font-weight: 600;
    white-space: nowrap;
  }

  .tab-indicator {
    width: 5px;
    height: 5px;
  }

  .header-right {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 10px;
  }

  /* Compact project picker in the header: shares the shared Dropdown chrome
     (same control as the Agents thinking-effort selector), only width-capped so
     it reads as a single header chip rather than stretching the bar. */
  :global(.chat-header__project-dropdown) {
    min-width: 150px;
    max-width: 220px;
  }

  @media (max-width: 640px) {
    .chat-header {
      height: auto;
      flex-wrap: wrap;
      padding: 10px 14px;
    }

    .agent-tabs {
      order: 2;
      width: 100%;
      height: 38px;
      flex-basis: 100%;
    }

    .header-right {
      margin-left: auto;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
  }
</style>
