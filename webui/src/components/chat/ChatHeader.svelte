<script>
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import { t } from '$lib/i18n.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import AgentActivityChips from './AgentActivityChips.svelte';
  import {
    agentActivityState,
    agentActivityTooltip,
  } from './agentActivityTooltip.js';

  // Pickers with more Agents than this, or with Project Teams, get a filter
  // field.
  const AGENT_FILTER_THRESHOLD = 6;
  // Room for longer names and unread counts under a compact trigger.
  const AGENT_PANEL_MIN_WIDTH = 260;
  const ACTIVITY_STATUSES = new Set(['running', 'unread']);
  // The Project groups this browser opened or closed in the picker.
  const EXPANDED_PROJECTS_KEY = 'vbot.chat.agentPicker.expandedProjects';

  let {
    titleId = 'chat-title',
    // Identity Agents, listed first.
    agents = [],
    // Project Teams, each a collapsible group after the Identity Agents:
    // [{ projectId, name, warning, members: [{ agent_id, display_name,
    // model, thinkingEffort }] }]. A Project without members is left out.
    projectGroups = [],
    // Per-Agent activity keyed by Agent address (a bare Identity Agent id or
    // `agent@project`): { status: 'running' | 'unread' | 'idle',
    // unreadCount, latestUnreadAt }.
    agentActivity = {},
    // The address of the Agent whose Session is shown.
    selectedAddress = '',
    // The name of a displayed Agent that is not in the picker (the hidden
    // Librarian, or an Agent deleted while its Session is shown): the picker
    // shows it in place of its "Select an agent" placeholder.
    displayedAgentName = '',
    loadingAgents = false,
    onSelectAgent = () => {},
  } = $props();

  let agentPicker = $state();
  let storedExpansion = $state(readStoredExpansion());
  // Groups toggled since the panel opened; the selected Agent's Project opens
  // with every panel unless toggled meanwhile.
  let toggledWhileOpen = $state(new Set());

  let identityEntries = $derived(
    agents.map((agent, index) =>
      describeAgent({
        address: agent.id,
        id: agent.id,
        name: agent.name || agent.id,
        model: agent.model,
        thinkingEffort: agent.thinking_effort,
        index,
      }),
    ),
  );
  let groups = $derived(
    projectGroups
      .filter((group) => group.members?.length > 0)
      .map((group) => {
        const projectName = group.name || group.projectId;
        const entries = group.members.map((member, index) => {
          const name = member.display_name || member.agent_id;
          return describeAgent({
            address: formatAgentAddress(member.agent_id, group.projectId),
            id: member.agent_id,
            name,
            fullName: t('chat.agentPicker.projectAgent', {
              agent: name,
              project: projectName,
            }),
            model: member.model,
            thinkingEffort: member.thinkingEffort,
            group: group.projectId,
            index,
          });
        });
        return {
          projectId: group.projectId,
          name: projectName,
          warning: Boolean(group.warning),
          entries,
          ...groupActivity(entries),
        };
      }),
  );
  let allEntries = $derived([
    ...identityEntries,
    ...groups.flatMap((group) => group.entries),
  ]);
  let selectedEntry = $derived(
    allEntries.find((entry) => entry.address === selectedAddress) ?? null,
  );
  let selectedProjectId = $derived(selectedEntry?.group ?? '');
  let agentOptions = $derived([
    ...byActivity(identityEntries).map(entryOption),
    ...groups.flatMap((group) => byActivity(group.entries).map(entryOption)),
  ]);
  let groupOptions = $derived(
    groups.map((group) => {
      const activity =
        group.status === 'idle'
          ? ''
          : agentActivityState(group.status, group.unreadCount);
      return {
        id: group.projectId,
        label: group.name,
        statusDot: group.status,
        badge: group.unreadCount > 0 ? group.unreadCount : '',
        ariaLabel: activity
          ? t('chat.agentPicker.projectActivity', {
              project: group.name,
              activity,
            })
          : '',
        tooltip: activity,
        warning: group.warning ? t('chat.agentPicker.projectScanWarning') : '',
      };
    }),
  );
  let expandedGroups = $derived(
    groups.filter((group) => isExpanded(group)).map((group) => group.projectId),
  );
  // Chips for every other Agent with activity: unread results first (newest
  // first), then running. The selected Agent's status is on the trigger.
  let chipAgents = $derived(
    [
      ...newestResultFirst(
        allEntries.filter(
          (entry) =>
            entry.status === 'unread' && entry.address !== selectedAddress,
        ),
      ),
      ...allEntries.filter(
        (entry) =>
          entry.status === 'running' && entry.address !== selectedAddress,
      ),
    ].map((entry) => ({
      id: entry.address,
      name: entry.fullName,
      status: entry.status,
      unreadCount: entry.unreadCount,
      label: entry.label,
      tooltip: entry.tooltip,
    })),
  );
  let searchable = $derived(
    groups.length > 0 || allEntries.length > AGENT_FILTER_THRESHOLD,
  );
  let pickerLabel = $derived(
    selectedEntry
      ? t('chat.agentPicker.label', {
          activity: selectedEntry.label,
        })
      : displayedAgentName || t('chat.selectAgent'),
  );
  let pickerProps = $derived({
    value: selectedEntry ? selectedAddress : '',
    options: agentOptions,
    placeholder: displayedAgentName || t('chat.selectAgent'),
    ariaLabel: pickerLabel,
    triggerClass: 'chat-header__agent-picker',
    triggerTooltip: selectedEntry?.tooltip ?? '',
    panelMinWidth: AGENT_PANEL_MIN_WIDTH,
    disabled: loadingAgents,
    onValueChange: (address) => onSelectAgent(address),
  });

  function describeAgent({
    address,
    id,
    name,
    fullName = name,
    model,
    thinkingEffort,
    group = '',
    index,
  }) {
    const activity = agentActivity[address] ?? {};
    const status = ACTIVITY_STATUSES.has(activity.status)
      ? activity.status
      : 'idle';
    const unreadCount =
      Number.isInteger(activity.unreadCount) && activity.unreadCount > 0
        ? activity.unreadCount
        : 0;
    return {
      address,
      name,
      fullName,
      group,
      status,
      unreadCount,
      latestUnreadAt: Number(activity.latestUnreadAt) || 0,
      index,
      label: agentActivityLabel(fullName, status, unreadCount),
      tooltip: agentActivityTooltip({
        name: fullName,
        id,
        status,
        unreadCount,
        model,
        thinkingEffort,
      }),
    };
  }

  function entryOption(entry) {
    return {
      value: entry.address,
      label: entry.name,
      triggerLabel: entry.fullName,
      group: entry.group,
      statusDot: entry.status,
      badge: entry.unreadCount > 0 ? entry.unreadCount : '',
      ariaLabel: entry.label,
      tooltip: entry.tooltip,
    };
  }

  // A closed group shows what its Agents do: running wins over unread.
  function groupActivity(entries) {
    const unreadCount = entries.reduce(
      (sum, entry) => sum + entry.unreadCount,
      0,
    );
    const status = entries.some((entry) => entry.status === 'running')
      ? 'running'
      : entries.some((entry) => entry.status === 'unread')
        ? 'unread'
        : 'idle';
    return { status, unreadCount };
  }

  // Picker order: running Agents, then unread ones (newest result first),
  // then the rest in roster order.
  function byActivity(entries) {
    return [
      ...entries.filter((entry) => entry.status === 'running'),
      ...newestResultFirst(
        entries.filter((entry) => entry.status === 'unread'),
      ),
      ...entries.filter((entry) => entry.status === 'idle'),
    ];
  }

  function newestResultFirst(entries) {
    return [...entries].sort(
      (left, right) =>
        right.latestUnreadAt - left.latestUnreadAt || left.index - right.index,
    );
  }

  // Open: the selected Agent's Project (unless toggled since the panel
  // opened), else the choice this browser remembers, else a Project whose
  // Agents are running or have unread results.
  function isExpanded(group) {
    if (
      group.projectId === selectedProjectId &&
      !toggledWhileOpen.has(group.projectId)
    ) {
      return true;
    }
    const stored = storedExpansion[group.projectId];
    return typeof stored === 'boolean' ? stored : group.status !== 'idle';
  }

  function handleGroupToggle(projectId, open) {
    toggledWhileOpen = new Set([...toggledWhileOpen, projectId]);
    storedExpansion = { ...storedExpansion, [projectId]: open };
    writeStoredExpansion(storedExpansion);
  }

  function handleOpenChange(open) {
    if (open) {
      toggledWhileOpen = new Set();
    }
  }

  function readStoredExpansion() {
    try {
      const parsed = JSON.parse(
        localStorage.getItem(EXPANDED_PROJECTS_KEY) || '{}',
      );
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? parsed
        : {};
    } catch {
      return {};
    }
  }

  function writeStoredExpansion(value) {
    try {
      localStorage.setItem(EXPANDED_PROJECTS_KEY, JSON.stringify(value));
    } catch {
      // localStorage unavailable (private browsing, storage quota)
    }
  }

  function agentActivityLabel(name, status, unreadCount) {
    if (status === 'running') {
      if (unreadCount === 1) {
        return t('chat.agentActivity.runningUnreadOne', { name });
      }
      if (unreadCount > 1) {
        return t('chat.agentActivity.runningUnreadCount', {
          name,
          count: unreadCount,
        });
      }
      return t('chat.agentActivity.running', { name });
    }
    if (status === 'unread') {
      if (unreadCount === 1) {
        return t('chat.agentActivity.unreadOne', {
          name,
        });
      }
      if (unreadCount > 1) {
        return t('chat.agentActivity.unreadCount', {
          name,
          count: unreadCount,
        });
      }
      return t('chat.agentActivity.unread', { name });
    }
    return t('chat.agentActivity.idle', { name });
  }
</script>

<header class="chat-header">
  <h2 id={titleId} class="chat-title">{t('chat.title')}</h2>
  <div class="agent-switcher">
    {#if allEntries.length > 0}
      {#if searchable}
        <SearchableDropdown
          bind:this={agentPicker}
          {...pickerProps}
          searchPlaceholder={t('chat.agentPicker.filter')}
          emptyLabel={t('chat.agentPicker.empty')}
          collapsibleGroups={groups.length > 0}
          groups={groupOptions}
          {expandedGroups}
          onGroupToggle={handleGroupToggle}
          onOpenChange={handleOpenChange}
        />
      {:else}
        <Dropdown bind:this={agentPicker} {...pickerProps} />
      {/if}
      <AgentActivityChips
        agents={chipAgents}
        disabled={loadingAgents}
        onSelect={(address) => onSelectAgent(address)}
        onShowMore={() => agentPicker?.open()}
      />
    {:else}
      <span class="agent-switcher__empty">
        {t('chat.noAgents')}
      </span>
    {/if}
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
    max-width: 280px;
    flex: 0 1 auto;
  }

  .agent-switcher__empty {
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    white-space: nowrap;
  }

  @media (max-width: 640px) {
    .chat-header {
      padding: 0 14px;
    }

    /* The picker takes the row; dot-only chips keep their natural width. */
    .agent-switcher :global(.chat-header__agent-picker) {
      min-width: 128px;
      max-width: none;
      flex: 1 1 128px;
    }
  }
</style>
