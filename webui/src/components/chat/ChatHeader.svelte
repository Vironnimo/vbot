<script>
  import { tick } from 'svelte';

  import { formatAgentAddress } from '$lib/agentAddress.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Button from '../ui/Button.svelte';
  import ContextMenu from '../ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from '../ui/contextMenu.js';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import {
    agentActivityState,
    agentActivityTooltip,
  } from './agentActivityTooltip.js';

  // The bar shows this many Identity Agents in roster order (the order set in
  // the Agents tab); the rest and every Project Team are under "All agents".
  const BAR_AGENT_LIMIT = 5;
  // Room for longer names and unread counts under the compact trigger.
  const AGENT_PANEL_MIN_WIDTH = 260;
  // The roster's inline padding, which leaves room for focus rings.
  const ROSTER_INSET = 2;
  const ACTIVITY_STATUSES = new Set(['running', 'unread']);
  // The Project groups this browser opened or closed in the picker.
  const EXPANDED_PROJECTS_KEY = 'vbot.chat.agentPicker.expandedProjects';

  let {
    titleId = 'chat-title',
    // Identity Agents in roster order.
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
    // Librarian, or an Agent deleted while its Session is shown): the bar
    // shows it as the current Agent.
    displayedAgentName = '',
    loadingAgents = false,
    onSelectAgent = () => {},
    // The Session list beside the chat: whether it is open, and its toggle.
    sessionListOpen = false,
    sessionListDisabled = false,
    onToggleSessionList = () => {},
    newSessionDisabled = false,
    onNewSession = () => {},
    // Actions for this Chat area at the bar's end, such as Split.
    actions = undefined,
    // Open in split view for an Agent tab (`{ label, open(target) }`); null
    // leaves the tabs without a context menu.
    otherArea = null,
  } = $props();

  // The open Agent tab menu (../ui/ContextMenu.svelte), or null.
  let agentMenu = $state(null);

  // A right click or the context menu key on an Agent tab offers to show the
  // Agent in the other Chat area.
  function openAgentMenu(entry, event) {
    if (!otherArea || event.defaultPrevented) return;
    event.preventDefault();
    agentMenu = {
      ...contextMenuAnchor(event),
      label: t('chat.agentMenu.label', { name: entry.fullName }),
      items: [
        {
          id: 'open-in-other-area',
          label: otherArea.label,
          onSelect: () => otherArea.open({ agentAddress: entry.address }),
        },
      ],
    };
  }

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
  let barEntries = $derived(identityEntries.slice(0, BAR_AGENT_LIMIT));
  // The bar Agents that fit on the bar's line, in order; the others wait
  // under "All agents" until the area is wide enough.
  let agentsElement = $state();
  let rosterElement = $state();
  let fittingCount = $state(BAR_AGENT_LIMIT);
  let visibleBarEntries = $derived(barEntries.slice(0, fittingCount));
  let selectedEntry = $derived(
    allEntries.find((entry) => entry.address === selectedAddress) ?? null,
  );
  // The displayed Agent keeps a place on the bar while it is shown.
  let extraEntry = $derived(
    selectedEntry && !visibleBarEntries.includes(selectedEntry)
      ? selectedEntry
      : null,
  );
  let selectedProjectId = $derived(selectedEntry?.group ?? '');
  // What the Agents reachable only through "All agents" are doing, shown on
  // its trigger.
  let hiddenActivity = $derived(
    groupActivity(
      allEntries.filter(
        (entry) => !visibleBarEntries.includes(entry) && entry !== extraEntry,
      ),
    ),
  );
  let showAllAgents = $derived(allEntries.length > visibleBarEntries.length);
  let allAgentsLabel = $derived(
    hiddenActivity.status === 'idle'
      ? t('chat.agentBar.allAgents')
      : t('chat.agentBar.allAgentsActivity', {
          activity: agentActivityState(
            hiddenActivity.status,
            hiddenActivity.unreadCount,
          ),
        }),
  );
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

  // Bar Agents wrap onto a hidden second line when the area is too narrow;
  // those on the first line are the ones shown, and the row ends after them
  // so the displayed Agent and "All agents" follow directly.
  function measureFittingAgents() {
    if (!rosterElement) {
      return;
    }
    rosterElement.style.width = '';
    const pills = Array.from(rosterElement.children);
    const firstLine = pills[0]?.offsetTop ?? 0;
    const wrapped = pills.findIndex((pill) => pill.offsetTop !== firstLine);
    fittingCount = wrapped === -1 ? pills.length : wrapped;
    if (wrapped > 0) {
      const last = pills[wrapped - 1];
      const end = last.offsetLeft + last.offsetWidth - rosterElement.offsetLeft;
      rosterElement.style.width = `${end + ROSTER_INSET}px`;
    }
  }

  // The Agents' row follows the area's width and each bar Agent its name or
  // initials; the roster's own width is set above, so it is not observed.
  $effect(() => {
    if (!agentsElement || typeof ResizeObserver === 'undefined') {
      return undefined;
    }
    void barEntries;
    const observer = new ResizeObserver(() => measureFittingAgents());
    observer.observe(agentsElement);
    for (const pill of rosterElement?.children ?? []) {
      observer.observe(pill);
    }
    return () => observer.disconnect();
  });

  $effect(() => {
    // Names, activity dots, the displayed Agent and "All agents" change what
    // fits.
    void barEntries.map((entry) => `${entry.name}:${entry.status}`).join();
    void extraEntry;
    void showAllAgents;
    void rosterElement;
    tick().then(measureFittingAgents);
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

{#snippet agentPill(entry, extra = false, wrapped = false)}
  <button
    type="button"
    class="agent-pill"
    class:agent-pill--extra={extra}
    class:agent-pill--wrapped={wrapped}
    data-initials={entry.name.slice(0, 2)}
    aria-pressed={entry.address === selectedAddress}
    aria-label={entry.label}
    use:tooltip={entry.tooltip}
    disabled={loadingAgents}
    onclick={() => onSelectAgent(entry.address)}
    oncontextmenu={(event) => openAgentMenu(entry, event)}
    onkeydown={(event) => {
      if (isContextMenuKey(event)) openAgentMenu(entry, event);
    }}
  >
    {#if entry.status !== 'idle'}
      <span
        class="agent-pill__dot tab-indicator tab-indicator--{entry.status}"
        aria-hidden="true"
      ></span>
    {/if}
    <span class="agent-pill__name">{extra ? entry.fullName : entry.name}</span>
  </button>
{/snippet}

{#snippet allAgentsTrigger()}
  <span class="chat-header__all-agents-icon" aria-hidden="true">
    {#if hiddenActivity.status !== 'idle'}
      <span
        class="chat-header__all-agents-dot tab-indicator tab-indicator--{hiddenActivity.status}"
      ></span>
    {/if}
    <svg viewBox="0 0 16 16" width="16" height="16">
      <circle cx="3.5" cy="8" r="1.3" />
      <circle cx="8" cy="8" r="1.3" />
      <circle cx="12.5" cy="8" r="1.3" />
    </svg>
  </span>
{/snippet}

<header class="chat-header">
  <h2 id={titleId} class="chat-title">{t('chat.title')}</h2>
  <div class="chat-header__session-actions">
    <Button
      variant="secondary"
      icon
      class="chat-header__action"
      ariaLabel={t('chat.agentBar.sessionList')}
      tooltip={sessionListOpen
        ? t('chat.agentBar.hideSessionList')
        : t('chat.agentBar.showSessionList')}
      aria-expanded={sessionListOpen}
      disabled={sessionListDisabled}
      onClick={() => onToggleSessionList()}
    >
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2" />
        <path d="M9 4v16" />
      </svg>
    </Button>
    <Button
      variant="secondary"
      icon
      class="chat-header__action"
      ariaLabel={t('chat.newSession')}
      tooltip={t('chat.newSession')}
      disabled={newSessionDisabled}
      onClick={() => onNewSession()}
    >
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path d="M12 5v14M5 12h14" />
      </svg>
    </Button>
  </div>
  <div
    bind:this={agentsElement}
    class="chat-header__agents"
    role="group"
    aria-label={t('chat.agentBar.label')}
  >
    {#if allEntries.length > 0 || displayedAgentName}
      <div bind:this={rosterElement} class="chat-header__roster">
        {#each barEntries as entry, index (entry.address)}
          {@render agentPill(entry, false, index >= fittingCount)}
        {/each}
      </div>
      {#if extraEntry}
        {@render agentPill(extraEntry, true)}
      {:else if !selectedEntry && displayedAgentName}
        <span class="agent-pill agent-pill--extra agent-pill--current">
          <span class="agent-pill__name">{displayedAgentName}</span>
        </span>
      {/if}
      {#if showAllAgents}
        <SearchableDropdown
          value={selectedEntry ? selectedAddress : ''}
          options={agentOptions}
          placeholder={t('chat.agentBar.allAgents')}
          ariaLabel={allAgentsLabel}
          triggerClass="chat-header__all-agents"
          triggerTooltip={allAgentsLabel}
          triggerContent={allAgentsTrigger}
          panelMinWidth={AGENT_PANEL_MIN_WIDTH}
          disabled={loadingAgents}
          searchPlaceholder={t('chat.agentPicker.filter')}
          emptyLabel={t('chat.agentPicker.empty')}
          collapsibleGroups={groups.length > 0}
          groups={groupOptions}
          {expandedGroups}
          onGroupToggle={handleGroupToggle}
          onOpenChange={handleOpenChange}
          onValueChange={(address) => onSelectAgent(address)}
        />
      {/if}
    {:else if !loadingAgents}
      <span class="chat-header__empty">{t('chat.noAgents')}</span>
    {/if}
  </div>
  {#if actions}
    <div class="chat-header__area-actions">
      {@render actions()}
    </div>
  {/if}
</header>

<ContextMenu menu={agentMenu} onClose={() => (agentMenu = null)} />

<style>
  .chat-header {
    display: flex;
    height: 50px;
    flex-shrink: 0;
    align-items: center;
    gap: 6px;
    padding: 0 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    container: agent-bar / inline-size;
  }

  .chat-title {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }

  .chat-header__session-actions,
  .chat-header__area-actions {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 2px;
  }

  .chat-header__session-actions {
    padding-right: 6px;
    border-right: 1px solid var(--border);
  }

  .chat-header__agents {
    display: flex;
    min-width: 0;
    flex: 1;
    align-items: center;
    gap: 4px;
  }

  /* In a narrow area only the roster Agents give way: the ones that do not
     fit wrap onto a clipped second line and wait under "All agents". The
     displayed Agent, "All agents" and the actions keep their place. */
  .chat-header__roster {
    display: flex;
    min-width: 0;
    height: 38px;
    flex: 0 1 auto;
    flex-wrap: wrap;
    align-content: flex-start;
    align-items: center;
    gap: 8px 4px;
    /* Room for focus rings inside the clipped row. */
    margin: -4px -2px;
    padding: 4px 2px;
    overflow: hidden;
  }

  .agent-pill--wrapped {
    visibility: hidden;
  }

  /* A tab per Agent; the status dot leads only while it runs or has an
     unread result. */
  .agent-pill {
    display: inline-flex;
    max-width: 180px;
    height: 30px;
    flex: 0 0 auto;
    align-items: center;
    gap: 8px;
    padding: 0 11px;
    border: 0;
    border-radius: var(--r-md);
    color: var(--text-med);
    background: transparent;
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    font-weight: 400;
    white-space: nowrap;
    transition:
      background 120ms ease,
      color 120ms ease;
  }

  .agent-pill:hover:not(:disabled) {
    color: var(--text-hi);
    background: var(--surface-2);
  }

  .agent-pill[aria-pressed='true'],
  .agent-pill--current {
    color: var(--text-hi);
    background: var(--surface-3);
    font-weight: 500;
  }

  .agent-pill:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  .agent-pill:disabled {
    cursor: default;
    opacity: 0.55;
  }

  /* An Agent on the bar only while it is displayed. */
  .agent-pill--extra {
    min-width: 0;
    flex-shrink: 0;
  }

  .agent-pill__name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chat-header__all-agents-icon {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .chat-header__all-agents-icon svg {
    fill: currentcolor;
  }

  .chat-header :global(.chat-header__all-agents) {
    width: auto;
    min-width: 0;
    flex: 0 0 auto;
  }

  .chat-header :global(.chat-header__all-agents .s-dropdown-trigger) {
    width: auto;
    height: 30px;
    min-height: 0;
    padding: 0 8px;
    border: 0;
    border-radius: var(--r-md);
    color: var(--text-med);
    background: transparent;
  }

  .chat-header
    :global(.chat-header__all-agents .s-dropdown-trigger:hover:not(:disabled)),
  .chat-header :global(.chat-header__all-agents.open .s-dropdown-trigger) {
    color: var(--text-hi);
    background: var(--surface-2);
  }

  .chat-header__empty {
    padding: 0 6px;
    color: var(--text-lo);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    white-space: nowrap;
  }

  /* Too narrow for names: the other Agents show their first two letters;
     the full name stays in their accessible label and tooltip. */
  @container agent-bar (max-width: 560px) {
    .chat-header__roster .agent-pill:not([aria-pressed='true']) {
      gap: 6px;
      padding: 0 9px;
    }

    .chat-header__roster
      .agent-pill:not([aria-pressed='true'])
      .agent-pill__name {
      display: none;
    }

    .chat-header__roster .agent-pill:not([aria-pressed='true'])::after {
      content: attr(data-initials);
    }
  }

  @media (max-width: 640px) {
    .chat-header {
      height: 56px;
      padding: 0 8px;
    }

    .chat-header__roster {
      height: 44px;
    }

    .agent-pill {
      max-width: 140px;
      height: 36px;
    }

    .chat-header :global(.chat-header__all-agents .s-dropdown-trigger) {
      min-width: 40px;
      height: 36px;
      justify-content: center;
    }
  }
</style>
