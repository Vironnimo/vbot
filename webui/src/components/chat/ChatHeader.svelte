<script>
  import { t } from '$lib/i18n.js';
  import { parseModelSelectionValue } from '$lib/modelSelection.js';
  import { tooltip } from '$lib/tooltip.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import AgentActivityChips from './AgentActivityChips.svelte';

  // Rosters larger than this get a filter field in the Agent picker.
  const AGENT_FILTER_THRESHOLD = 6;
  // Room for longer names and unread counts under a compact trigger.
  const AGENT_PANEL_MIN_WIDTH = 240;
  const ACTIVITY_STATUSES = new Set(['running', 'unread']);

  let {
    titleId = 'chat-title',
    agents = [],
    // Per-Agent activity keyed by Agent id:
    // { status: 'running' | 'unread' | 'idle', unreadCount, latestUnreadAt }.
    agentActivity = {},
    selectedAgentId = '',
    loadingAgents = false,
    // Project context for the compact project picker that lives in the header
    // (left of the Sessions button). "No project" is Personal/identity chat.
    projects = [],
    selectedProjectId = '',
    onSelectProject = () => {},
    onSelectAgent = () => {},
  } = $props();

  let agentPicker = $state();

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

  let agentEntries = $derived(
    agents.map((agent, index) => describeAgent(agent, index)),
  );
  let selectedEntry = $derived(
    agentEntries.find((entry) => entry.id === selectedAgentId) ?? null,
  );
  // Picker order: running Agents, then unread ones (newest result first),
  // then the rest in roster order.
  let agentOptions = $derived(
    [
      ...agentEntries.filter((entry) => entry.status === 'running'),
      ...newestResultFirst(
        agentEntries.filter((entry) => entry.status === 'unread'),
      ),
      ...agentEntries.filter((entry) => entry.status === 'idle'),
    ].map((entry) => ({
      value: entry.id,
      label: entry.name,
      statusDot: entry.status,
      badge: entry.unreadCount > 0 ? entry.unreadCount : '',
      ariaLabel: entry.label,
    })),
  );
  // Chips for every other Agent with activity: unread results first (newest
  // first), then running. The selected Agent's status is on the trigger.
  let chipAgents = $derived(
    [
      ...newestResultFirst(
        agentEntries.filter(
          (entry) => entry.status === 'unread' && entry.id !== selectedAgentId,
        ),
      ),
      ...agentEntries.filter(
        (entry) => entry.status === 'running' && entry.id !== selectedAgentId,
      ),
    ].map((entry) => ({
      id: entry.id,
      name: entry.name,
      status: entry.status,
      unreadCount: entry.unreadCount,
      label: entry.label,
      tooltip: entry.tooltip,
    })),
  );
  let pickerLabel = $derived(
    selectedEntry
      ? t('chat.agentPicker.label', 'Select agent ({activity})', {
          activity: selectedEntry.label,
        })
      : t('chat.selectAgent', 'Select agent'),
  );
  let pickerProps = $derived({
    value: selectedAgentId,
    options: agentOptions,
    placeholder: t('chat.selectAgent', 'Select agent'),
    ariaLabel: pickerLabel,
    triggerClass: 'chat-header__agent-picker',
    triggerTooltip: selectedEntry?.tooltip ?? '',
    panelMinWidth: AGENT_PANEL_MIN_WIDTH,
    disabled: loadingAgents,
    onValueChange: (agentId) => onSelectAgent(agentId),
  });

  function describeAgent(agent, index) {
    const activity = agentActivity[agent.id] ?? {};
    const status = ACTIVITY_STATUSES.has(activity.status)
      ? activity.status
      : 'idle';
    const unreadCount =
      Number.isInteger(activity.unreadCount) && activity.unreadCount > 0
        ? activity.unreadCount
        : 0;
    const name = agent.name || agent.id;
    const label = agentActivityLabel(name, status, unreadCount);
    return {
      id: agent.id,
      name,
      status,
      unreadCount,
      latestUnreadAt: Number(activity.latestUnreadAt) || 0,
      index,
      label,
      tooltip: agentActivityTooltip(label, agent.model),
    };
  }

  function newestResultFirst(entries) {
    return [...entries].sort(
      (left, right) =>
        right.latestUnreadAt - left.latestUnreadAt || left.index - right.index,
    );
  }

  function agentActivityLabel(name, status, unreadCount) {
    if (status === 'running') {
      if (unreadCount === 1) {
        return t(
          'chat.agentActivity.runningUnreadOne',
          '{name}: Running, 1 unread result',
          { name },
        );
      }
      if (unreadCount > 1) {
        return t(
          'chat.agentActivity.runningUnreadCount',
          '{name}: Running, {count} unread results',
          { name, count: unreadCount },
        );
      }
      return t('chat.agentActivity.running', '{name}: Running', { name });
    }
    if (status === 'unread') {
      if (unreadCount === 1) {
        return t('chat.agentActivity.unreadOne', '{name}: 1 unread result', {
          name,
        });
      }
      if (unreadCount > 1) {
        return t(
          'chat.agentActivity.unreadCount',
          '{name}: {count} unread results',
          { name, count: unreadCount },
        );
      }
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
  <h2 id={titleId} class="chat-title">{t('chat.title', 'Chat')}</h2>
  <div class="agent-switcher">
    {#if showPersonalLabel}
      <span
        class="agent-switcher__personal-label"
        use:tooltip={t(
          'chat.personalBarHint',
          'Your personal agents — available with or without a project.',
        )}
      >
        {t('chat.personalBarLabel', 'Personal')}
      </span>
    {/if}
    {#if agents.length > 0}
      {#if agents.length > AGENT_FILTER_THRESHOLD}
        <SearchableDropdown
          bind:this={agentPicker}
          {...pickerProps}
          searchPlaceholder={t('chat.agentPicker.filter', 'Filter agents…')}
          emptyLabel={t('chat.agentPicker.empty', 'No agents match')}
        />
      {:else}
        <Dropdown bind:this={agentPicker} {...pickerProps} />
      {/if}
      <AgentActivityChips
        agents={chipAgents}
        disabled={loadingAgents}
        onSelect={(agentId) => onSelectAgent(agentId)}
        onShowMore={() => agentPicker?.open()}
      />
    {:else}
      <span class="agent-switcher__empty">
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

  .agent-switcher {
    display: flex;
    min-width: 0;
    height: 100%;
    flex: 1;
    align-items: center;
    gap: 8px;
  }

  /* The picker keeps its own width; activity chips give way first. */
  .agent-switcher :global(.chat-header__agent-picker) {
    width: auto;
    min-width: 150px;
    max-width: 240px;
    flex: 0 1 auto;
  }

  .agent-switcher__empty {
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    white-space: nowrap;
  }

  /* Bold "Personal" label before the Agent picker, mirroring the project-name
     label on the team bar below (.chat-view__project-team-name) so the two
     bars read as a matched pair when a project is selected. */
  .agent-switcher__personal-label {
    display: flex;
    height: 100%;
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

    .agent-switcher {
      order: 2;
      width: 100%;
      height: 38px;
      flex-basis: 100%;
    }

    /* The picker takes the row; dot-only chips keep their natural width. */
    .agent-switcher :global(.chat-header__agent-picker) {
      min-width: 128px;
      max-width: none;
      flex: 1 1 128px;
    }

    .header-right {
      margin-left: auto;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
  }
</style>
