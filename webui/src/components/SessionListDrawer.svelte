<script>
  import { t } from '$lib/i18n.js';
  import { portal } from '$lib/dropdownPanel.js';
  import { useNavigation } from '$lib/navigation.svelte.js';
  import Toggle from './ui/Toggle.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import {
    asText,
    autofocusRename,
    sessionHoverDetails,
    unreadRunDetails,
    sessionMarker,
  } from './sessions/presentation.js';
  import { tooltip } from '$lib/tooltip.js';
  import { sessionKindIcon } from './sessions/kindIcons.js';
  import SessionKindIcon from './sessions/SessionKindIcon.svelte';
  import {
    sessionDisplayName,
    projectGroupKey,
    createSessionListFilters,
    overlayLiveSessionActivity,
    visibleSessionsForSelection,
  } from '$lib/sessionListView.js';
  import ContextMenu from './ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from './ui/contextMenu.js';
  import SortableList from './ui/SortableList.svelte';
  import { moveItem } from './ui/sortable.js';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import ArchiveDeleteOption from './archive/ArchiveDeleteOption.svelte';
  import Modal from './ui/Modal.svelte';
  import CompactionPolicyEditor from './compaction/CompactionPolicyEditor.svelte';
  import { untrack } from 'svelte';
  import { formatAgentAddress, parseAgentAddress } from '$lib/agentAddress.js';
  import { createSessionList } from './sessions/list.svelte.js';
  import { createSessionActions } from './sessions/actions.svelte.js';
  import { createSessionMenus } from './sessions/menus.svelte.js';
  import './sessions/sessions.css';

  let {
    agentId = '',
    currentSessionId = '',
    // Bumped when Session-list continuity is uncertain (replay gap or server
    // restart): the list reloads whatever it shows.
    reloadToken = 0,
    // App's bounded window of `resource_changed(kind:"sessions")` scopes. The
    // list reloads (coalesced) only for scopes naming an Agent it lists, so a
    // new or renamed Session from another window appears automatically.
    invalidations = [],
    // Roster of addressable agents ({ address, name }) the "All agents"
    // filter lists sessions for — the same set the Chat agent bars show.
    agents = [],
    // Lean projection of Chat's live Run/activity state. It overlays the last
    // session.list snapshot so an already-open drawer reacts immediately.
    liveActivity = [],
    // Persisted filter state from the parent (ChatView). On first mount this
    // is null and the filters default; on remount (panel reopened) the
    // previously chosen filters are restored so toggling the panel doesn't
    // reset the user's filter selection.
    initialFilters = null,
    // Called with the full filter object whenever a filter changes, so the
    // parent can persist it across panel close/reopen cycles.
    onFiltersChange = () => {},
    // Called with (sessionId, agentAddress, isSubAgentSession) when a row is
    // picked — the address routes cross-agent selections, the flag drives the
    // sub-agent footer banner in ChatView.
    onSessionSelected = () => {},
    // Open in split view for a row (`{ label, open(target) }` with target
    // `{ agentAddress, sessionId, subAgent }`); null leaves it out of the
    // row menu.
    otherArea = null,
    // Called after a successful delete with { deletedSessionId, nextSessionId,
    // agentAddress } so ChatView can navigate if it was viewing the removed
    // session (#2).
    onSessionDeleted = () => {},
    // Optional focus intent for a confirmed delete. Captured after the dialog
    // restores focus, passed to completion, and released on success or error.
    onSessionDeleteStarted = null,
    // Called with (agentAddress, sessionId, effectivePolicy) after a Session
    // Compaction Policy is saved, so Chat applies it without another read.
    onCompactionPolicyChange = () => {},
    // Registered Projects ({ project_id, display_name }) in the user's
    // order: they name and order the groups of the by-Project view.
    projects = [],
    // Stores a new Project order (every Project id, in order); moving a
    // group calls it. Returns a promise that settles once it is saved.
    onReorderProjects = async () => {},
    // Every Project's Team ({ projectId, members: [{ agent_id, display_name }] })
    // for the Agent names of Team rows and the "+" Agent choice.
    projectTeams = [],
    // Called with (projectId, agentAddress) when a group's "+" asks for a new
    // Session of that Agent in that Project (null: the Workspace).
    onNewSessionInProject = () => {},
  } = $props();
  const menus = createSessionMenus();
  const uid = $props.id();
  // The open row menu (./ui/ContextMenu.svelte) and the row it belongs to.
  let rowMenu = $state(null);
  let rowMenuKey = $state(null);
  const closeRowMenu = () => {
    rowMenu = null;
    rowMenuKey = null;
  };
  const actions = createSessionActions({
    get agentId() {
      return agentId;
    },
    get closeMenu() {
      return closeRowMenu;
    },
    get loadSessions() {
      return list.loadSessions;
    },
    get onSessionDeleted() {
      return onSessionDeleted;
    },
    get onSessionDeleteStarted() {
      return onSessionDeleteStarted;
    },
    get onCompactionPolicyChange() {
      return onCompactionPolicyChange;
    },
  });

  let filters = $state({
    ...createSessionListFilters(),
    ...untrack(() => initialFilters),
  });
  let activeFilterCount = $derived(
    Number(filters.channels) +
      Number(filters.subagents) +
      Number(filters.memoryReflections) +
      Number(filters.skillReflections) +
      Number(filters.cron),
  );

  // Deduplicated roster the All-agents filter loads sessions for.
  let rosterAgents = $derived.by(() => {
    const seenAddresses = [];
    const roster = [];
    for (const entry of Array.isArray(agents) ? agents : []) {
      const address = asText(entry?.address);
      if (!address || seenAddresses.includes(address)) {
        continue;
      }
      seenAddresses.push(address);
      roster.push({ address, name: asText(entry?.name) || address });
    }
    return roster;
  });

  const list = createSessionList({
    get agentId() {
      return agentId;
    },
    get currentSessionId() {
      return currentSessionId;
    },
    get filters() {
      return filters;
    },
    get rosterAgents() {
      return rosterAgents;
    },
    get reloadToken() {
      return reloadToken;
    },
    get invalidations() {
      return invalidations;
    },
    get liveActivity() {
      return liveActivity;
    },
    get onSessionSelected() {
      return onSessionSelected;
    },
  });

  // Names of every Agent a by-Project row can belong to: the roster's
  // Identity Agents and each Project's Team members.
  let agentNames = $derived.by(() => {
    const names = {};
    for (const entry of rosterAgents) {
      names[entry.address] = entry.name;
    }
    for (const team of Array.isArray(projectTeams) ? projectTeams : []) {
      for (const member of team?.members ?? []) {
        const address = formatAgentAddress(member.agent_id, team.projectId);
        names[address] ??= member.display_name || member.agent_id;
      }
    }
    return names;
  });

  let projectNames = $derived(
    Object.fromEntries(
      (Array.isArray(projects) ? projects : []).map((project) => [
        project.project_id,
        project.display_name || project.project_id,
      ]),
    ),
  );

  let displayedGroups = $derived(
    list.projectState.groups.map((group) => {
      const sessions = overlayLiveSessionActivity(
        group.sessions,
        liveActivity,
        null,
      ).map((session) => ({
        ...session,
        // Named in the row's details card, not on the row.
        owner_name:
          agentNames[session.agent_address] ||
          parseAgentAddress(session.agent_address).agentId,
      }));
      return {
        ...group,
        key: projectGroupKey(group.projectId),
        name:
          group.projectId === null
            ? t('sessions.projects.other')
            : projectNames[group.projectId] || group.projectId,
        sessions: visibleSessionsForSelection(sessions, {
          filters,
          selectedSessionId: currentSessionId,
        }),
      };
    }),
  );

  // The registered Projects' groups in the user's Project order, then the
  // group of the Sessions in no registered Project.
  let projectGroups = $derived.by(() => {
    const position = Object.fromEntries(
      (Array.isArray(projects) ? projects : []).map((project, index) => [
        project.project_id,
        index,
      ]),
    );
    const unknown = Number.MAX_SAFE_INTEGER;
    return displayedGroups
      .filter((group) => group.projectId !== null)
      .map((group, index) => ({ group, index }))
      .sort(
        (a, b) =>
          (position[a.group.projectId] ?? unknown) -
            (position[b.group.projectId] ?? unknown) || a.index - b.index,
      )
      .map(({ group }) => group);
  });
  let otherGroup = $derived(
    displayedGroups.find((group) => group.projectId === null) ?? null,
  );

  // Moving a group reorders every registered Project; Projects without a
  // group yet keep their order after the moved ones.
  const reorderProjectGroups = (from, to) => {
    const registered = (Array.isArray(projects) ? projects : []).map(
      (project) => project.project_id,
    );
    const moved = moveItem(projectGroups, from, to)
      .map((group) => group.projectId)
      .filter((projectId) => registered.includes(projectId));
    return onReorderProjects([
      ...moved,
      ...registered.filter((projectId) => !moved.includes(projectId)),
    ]);
  };

  const groupCollapsed = (key) => filters.collapsedProjects.includes(key);

  const toggleGroup = (key) =>
    setFilter(
      'collapsedProjects',
      groupCollapsed(key)
        ? filters.collapsedProjects.filter((entry) => entry !== key)
        : [...filters.collapsedProjects, key],
    );

  // The Agent a group's "+" starts with: the displayed Agent when it can
  // work in that Project - an Identity Agent everywhere, a Team member only
  // in its own Project - else '' (the user chooses).
  const directNewSessionAgent = (projectId) => {
    const address = asText(agentId);
    const { projectId: teamProjectId } = parseAgentAddress(address);
    if (teamProjectId) {
      return teamProjectId === projectId ? address : '';
    }
    return rosterAgents.some((entry) => entry.address === address)
      ? address
      : '';
  };

  // The Agents that can work in a Project group: every Identity Agent, then
  // that Project's Team.
  const newSessionChoices = (projectId) => {
    const identity = rosterAgents
      .filter((entry) => !parseAgentAddress(entry.address).projectId)
      .map((entry) => ({ ...entry, group: 'identity' }));
    const team =
      projectId === null
        ? []
        : ((Array.isArray(projectTeams) ? projectTeams : []).find(
            (entry) => entry.projectId === projectId,
          )?.members ?? []);
    return [
      ...identity,
      ...team.map((member) => ({
        address: formatAgentAddress(member.agent_id, projectId),
        name: member.display_name || member.agent_id,
        group: 'team',
      })),
    ];
  };

  const startNewSessionInGroup = (group, event) => {
    const direct = directNewSessionAgent(group.projectId);
    if (direct) {
      onNewSessionInProject(group.projectId, direct);
      return;
    }
    menus.closeFilterMenu();
    rowMenuKey = null;
    rowMenu = {
      ...contextMenuAnchor(event),
      label: t('sessions.projects.chooseAgent', { project: group.name }),
      items: newSessionChoices(group.projectId).map((choice) => ({
        id: choice.address,
        label: choice.name,
        group: choice.group,
        onSelect: () => onNewSessionInProject(group.projectId, choice.address),
      })),
    };
  };

  // The header names whose Sessions the list shows.
  let headerTitle = $derived(
    filters.byProject
      ? t('sessions.projects.title')
      : filters.allAgents
        ? t('sessions.filters.allAgents')
        : rosterAgents.find((entry) => entry.address === asText(agentId))
            ?.name || t('sessions.title'),
  );

  const SESSION_FILTER_ROWS = [
    {
      key: 'channels',
      label: () => t('sessions.filters.channels'),
    },
    {
      key: 'subagents',
      label: () => t('sessions.filters.subagents'),
    },
    {
      key: 'memoryReflections',
      label: () => t('sessions.filters.memoryReflections'),
    },
    {
      key: 'skillReflections',
      label: () => t('sessions.filters.skillReflections'),
    },
    {
      key: 'cron',
      label: () => t('sessions.filters.cron'),
    },
  ];

  // The filter trigger names the hidden categories it currently shows.
  function filterTooltip() {
    const active = SESSION_FILTER_ROWS.filter((row) => filters[row.key]);
    if (active.length === 0) {
      return {
        title: t('sessions.filtersAria'),
        text: t('sessions.filters.noneActive'),
      };
    }
    return {
      title: t('sessions.filters.activeTitle'),
      text: active.map((row) => row.label()).join('\n'),
    };
  }

  const setFilter = (key, checked) => {
    filters = { ...filters, [key]: checked };
    onFiltersChange(filters);
  };

  // A right click on the row, the context menu key on its button and its
  // "..." button open the same menu. A row being renamed keeps the native
  // menu of its input.
  const openRowMenu = (session, event) => {
    if (
      event.defaultPrevented ||
      (actions.editingSessionId === session.id &&
        actions.editingAgentAddress ===
          (session.agent_address || asText(agentId)))
    ) {
      return;
    }
    event.preventDefault();
    menus.closeFilterMenu();
    rowMenuKey = sessionRowKey(session);
    const openElsewhere = otherArea
      ? [
          {
            id: 'open-in-other-area',
            label: otherArea.label,
            group: 'open',
            onSelect: () =>
              otherArea.open({
                agentAddress: session.agent_address || asText(agentId),
                sessionId: session.id,
                subAgent: session.is_subagent_session === true,
              }),
          },
        ]
      : [];
    rowMenu = {
      ...contextMenuAnchor(event),
      label: t('sessions.actions'),
      items: [
        ...openElsewhere,
        {
          id: 'rename',
          label: t('sessions.rename'),
          onSelect: () => actions.startRename(session),
        },
        {
          id: 'compaction-policy',
          label: t('sessions.compactionPolicy'),
          onSelect: () => actions.startPolicyEdit(session),
        },
        {
          id: 'delete',
          label: t('sessions.delete'),
          danger: true,
          group: 'delete',
          onSelect: () => actions.requestDelete(session),
        },
      ],
    };
  };

  // Close the filter dropdown on an outside click or Escape, mirroring the
  // Dropdown primitive. Its panel is portaled, so the original trigger area
  // and the document-root panel count as inside.
  const handleDocumentMouseDown = (event) => {
    if (
      event.target instanceof Element &&
      menus.filterMenuOpen &&
      (event.target.closest('.session-drawer__filter') ||
        menus.filterMenuElement?.contains(event.target))
    ) {
      return;
    }
    menus.closeFilterMenu();
  };

  const handleDocumentKeyDown = (event) => {
    if (event.key === 'Escape') {
      menus.closeFilterMenu();
    }
  };

  const handleWindowScroll = (event) => {
    if (
      event.target instanceof Node &&
      menus.filterMenuElement?.contains(event.target)
    ) {
      return;
    }
    menus.closeFilterMenu();
  };

  $effect(() => {
    if (!menus.filterMenuOpen) {
      return undefined;
    }

    window.addEventListener('scroll', handleWindowScroll, true);
    return () => {
      window.removeEventListener('scroll', handleWindowScroll, true);
    };
  });

  // Back and Forward close an open filter menu first.
  const shell = useNavigation();
  $effect(() => {
    if (!menus.filterMenuOpen) return;
    return shell?.registerLayer({
      close: () => menus.closeFilterMenu(),
    });
  });

  const handleListScroll = (event) => {
    if (!list.hasMoreToDisplay || list.loadingMore) {
      return;
    }
    const el = event.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 60) {
      list.loadMoreSessions();
    }
  };

  const sessionRowKey = (session) =>
    `${session.agent_address || asText(agentId)}::${session.id}`;
</script>

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

<svelte:window onresize={menus.closeFilterMenu} />

<aside class="session-drawer" aria-label={t('sessions.title')}>
  <div class="session-drawer__header">
    <h3 class="session-drawer__title">{headerTitle}</h3>
    <div class="session-drawer__filter">
      <button
        type="button"
        class="session-drawer__icon-button session-drawer__by-project"
        class:session-drawer__filter-trigger--active={filters.byProject}
        aria-label={t('sessions.projects.toggle')}
        aria-pressed={filters.byProject}
        use:tooltip={filters.byProject
          ? t('sessions.projects.toggleOn')
          : t('sessions.projects.toggleOff')}
        onclick={() => setFilter('byProject', !filters.byProject)}
      >
        <svg
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path
            d="M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h3.6l1.8 2h6.6a1.5 1.5 0 0 1 1.5 1.5v7a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5z"
          />
        </svg>
      </button>
      {#if !filters.byProject}
        <button
          type="button"
          class="session-drawer__icon-button session-drawer__all-agents"
          class:session-drawer__filter-trigger--active={filters.allAgents}
          aria-label={t('sessions.filters.allAgents')}
          aria-pressed={filters.allAgents}
          use:tooltip={filters.allAgents
            ? t('sessions.filters.allAgentsOn')
            : t('sessions.filters.allAgentsOff')}
          onclick={() => setFilter('allAgents', !filters.allAgents)}
        >
          <svg
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <circle cx="7" cy="6" r="2.5" />
            <path
              d="M2 16v-1.5a5 5 0 0 1 10 0V16M13 3.7a2.5 2.5 0 0 1 0 4.6M15 10a4 4 0 0 1 3 4v2"
            />
          </svg>
        </button>
      {/if}
      <button
        type="button"
        class="session-drawer__icon-button session-drawer__filter-trigger"
        class:session-drawer__filter-trigger--active={activeFilterCount > 0}
        class:session-drawer__filter-trigger--open={menus.filterMenuOpen}
        aria-label={t('sessions.filtersAria')}
        aria-haspopup="menu"
        aria-expanded={menus.filterMenuOpen}
        use:tooltip={filterTooltip}
        onclick={(event) => menus.toggleFilterMenu(event.currentTarget)}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path
            d="M1.75 2.75h12.5M3.75 8h8.5M6.25 13.25h3.5"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
          />
        </svg>
        {#if activeFilterCount > 0}
          <span
            class="session-drawer__filter-count"
            aria-label={t('sessions.filtersActive', {
              count: activeFilterCount,
            })}
          >
            {activeFilterCount}
          </span>
        {/if}
      </button>
      {#if menus.filterMenuOpen}
        <div
          bind:this={menus.filterMenuElement}
          use:portal
          class="session-drawer__filter-menu"
          role="menu"
          data-placement={menus.filterMenuPlacement}
          data-positioning="fixed"
          style={menus.filterMenuStyle}
        >
          {#each SESSION_FILTER_ROWS as filterRow (filterRow.key)}
            <div
              class="session-drawer__filter-row"
              role="menuitemcheckbox"
              aria-checked={filters[filterRow.key]}
            >
              <span>{filterRow.label()}</span>
              <Toggle
                size="sm"
                checked={filters[filterRow.key]}
                ariaLabel={filterRow.label()}
                onChange={(checked) => setFilter(filterRow.key, checked)}
              />
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>

  {#if actions.actionError}
    <p class="session-drawer__state session-drawer__state--error" role="alert">
      {actions.actionError}
    </p>
  {/if}

  {#if filters.byProject}
    {#if list.projectState.error}
      <Banner variant="error" class="session-drawer__load-error" role="alert">
        <span>{list.projectState.error}</span>
        <Button
          variant="secondary"
          disabled={list.projectState.loading}
          onClick={() => list.loadSessions()}
        >
          {t('common.retry')}
        </Button>
      </Banner>
    {/if}
    {#if list.projectState.loading && list.projectState.groups.length === 0}
      <p class="session-drawer__state">
        {t('sessions.loading')}
      </p>
    {:else}
      <div class="session-drawer__groups">
        <SortableList
          tag="div"
          itemTag="div"
          class="session-drawer__project-groups"
          items={projectGroups}
          getKey={(group) => group.key}
          getLabel={(group) => group.name}
          aria-label={t('sessions.projects.title')}
          onReorder={reorderProjectGroups}
        >
          {#snippet item(group)}
            {@render projectGroup(group)}
          {/snippet}
        </SortableList>
        {#if otherGroup}
          {@render projectGroup(otherGroup)}
        {/if}
      </div>
    {/if}
  {:else if list.sessionState.error}
    <Banner variant="error" class="session-drawer__load-error" role="alert">
      <span>{list.sessionState.error}</span>
      <Button
        variant="secondary"
        disabled={list.sessionState.loading || !agentId}
        onClick={() => list.loadSessions()}
      >
        {t('common.retry')}
      </Button>
    </Banner>
  {:else if list.sessionState.loading && list.sessionState.sessions.length === 0}
    <p class="session-drawer__state">
      {t('sessions.loading')}
    </p>
  {:else if list.sessionState.sessions.length === 0}
    <EmptyState
      density="compact"
      class="session-drawer__empty-layout"
      title={t('chat.sessions.emptyTitle')}
      description={filters.allAgents
        ? t('sessions.no_sessions_all')
        : t('sessions.no_sessions')}
    />
  {:else if list.visibleSessions.length === 0}
    <EmptyState
      density="compact"
      class="session-drawer__empty-layout"
      title={t('sessions.noImportantTitle')}
      description={t('sessions.noImportantDescription')}
    />
  {:else}
    <ul class="session-drawer__list" onscroll={handleListScroll}>
      {#each list.visibleSessions as session (sessionRowKey(session))}
        {@render sessionRow(session)}
      {/each}
    </ul>
    {#if list.hasMoreToDisplay}
      <p class="session-drawer__more-hint">
        {t('sessions.moreHint', {
          count: list.remainingSessionCount,
        })}
      </p>
    {/if}
  {/if}
</aside>

{#snippet projectGroup(group)}
  {@const collapsed = groupCollapsed(group.key)}
  {@const direct = directNewSessionAgent(group.projectId)}
  <section
    class="session-group"
    aria-labelledby={`${uid}-group-${group.key || '~other'}`}
  >
    <div class="session-group__header">
      <button
        type="button"
        class="session-group__toggle"
        aria-expanded={!collapsed}
        onclick={() => toggleGroup(group.key)}
      >
        <svg
          class="session-group__chevron"
          viewBox="0 0 16 16"
          aria-hidden="true"
        >
          <path
            d="m6 4 4 4-4 4"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <span
          class="session-group__name"
          id={`${uid}-group-${group.key || '~other'}`}
        >
          {group.name}
        </span>
      </button>
      <button
        type="button"
        class="session-drawer__icon-button session-group__new"
        aria-label={t('sessions.projects.newSession', {
          project: group.name,
        })}
        aria-haspopup={direct ? undefined : 'menu'}
        use:tooltip={t('sessions.projects.newSession', {
          project: group.name,
        })}
        onclick={(event) => startNewSessionInGroup(group, event)}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path
            d="M8 3.5v9M3.5 8h9"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
          />
        </svg>
      </button>
    </div>
    {#if !collapsed}
      <!-- Only the header moves the group; the Sessions keep their own
             pointer and keyboard behavior. -->
      <div class="session-group__body" data-sortable-ignore>
        {#if group.sessions.length > 0}
          <ul class="session-drawer__list session-group__list">
            {#each group.sessions as session (sessionRowKey(session))}
              {@render sessionRow(session, group.sessions)}
            {/each}
          </ul>
        {:else}
          <p class="session-group__empty">
            {t('sessions.projects.empty')}
          </p>
        {/if}
        {#if group.nextCursor !== null}
          <button
            type="button"
            class="session-group__more"
            disabled={list.loadingMoreGroupKey !== null}
            onclick={() => list.loadMoreInGroup(group)}
          >
            {t('sessions.projects.showMore', {
              count: Math.max(group.totalCount - group.sessions.length, 0),
            })}
          </button>
        {/if}
      </div>
    {/if}
  </section>
{/snippet}

{#snippet sessionRow(session, rowSessions = list.sessionsWithLiveActivity)}
  {@const marker = sessionMarker(session)}
  <!-- Right click anywhere on the row opens its menu; the context menu key
         on the row's button is the keyboard path. -->
  <li
    class="session-row"
    class:session-row--marked={marker !== null}
    class:session-row--menu-open={rowMenuKey === sessionRowKey(session)}
    class:session-row--editing={actions.editingSessionId === session.id &&
      actions.editingAgentAddress ===
        (session.agent_address || asText(agentId))}
    oncontextmenu={(event) => openRowMenu(session, event)}
  >
    {#if actions.editingSessionId === session.id && actions.editingAgentAddress === (session.agent_address || asText(agentId))}
      <div class="session-row__edit">
        <input
          class="session-row__edit-input"
          type="text"
          value={actions.editValue}
          maxlength={actions.SESSION_TITLE_MAX_LENGTH}
          placeholder={t('sessions.rename_placeholder')}
          aria-label={t('sessions.rename_label')}
          disabled={actions.renameSaving}
          oninput={(event) => (actions.editValue = event.currentTarget.value)}
          onkeydown={actions.handleRenameKeydown}
          use:autofocusRename
        />
        {#if actions.renameError}
          <p class="session-row__edit-error" role="alert">
            {actions.renameError}
          </p>
        {/if}
      </div>
    {:else}
      <button
        type="button"
        class:session-row__select--active={list.rowSelected(session)}
        class="session-row__select"
        onclick={() => list.handleSelectSession(session)}
        onkeydown={(event) => {
          if (isContextMenuKey(event)) openRowMenu(session, event);
        }}
        use:tooltip={() =>
          sessionHoverDetails(session, {
            sessions: rowSessions,
            agents: rosterAgents,
          })}
      >
        <div class="session-row__heading">
          <!-- One status slot before the title: running wins over an
                 unread result. -->
          {#if session.has_active_run}
            <span
              class="session-row__status session-row__active-dot"
              aria-label={t('sessions.activeRun')}
              use:tooltip={t('sessions.activeRunHint')}
            >
              <span
                class="tab-indicator tab-indicator--running"
                aria-hidden="true"
              ></span>
            </span>
          {:else if session.has_unread_completion && session.id !== currentSessionId}
            <span
              class="session-row__status session-row__unread"
              aria-label={t('sessions.unreadCompletion')}
              use:tooltip={() => unreadRunDetails(session)}
            >
              <span
                class="tab-indicator tab-indicator--unread"
                aria-hidden="true"
              ></span>
            </span>
          {/if}
          <p class="session-row__name">
            {session.display_name || sessionDisplayName(session)}
          </p>
        </div>
        {#if marker}
          <span
            class="session-row__marker"
            role="img"
            aria-label={marker.label}
            data-session-marker={marker.kind}
          >
            <SessionKindIcon icon={sessionKindIcon(marker)} />
          </span>
        {/if}
        {#if session.agent_name}
          <span class="session-row__agent">{session.agent_name}</span>
        {/if}
      </button>
      <Button
        variant="tertiary"
        icon
        class="session-row__menu-trigger"
        ariaLabel={t('sessions.actions')}
        aria-haspopup="menu"
        aria-expanded={rowMenuKey === sessionRowKey(session)}
        onClick={(event) => openRowMenu(session, event)}
      >
        <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <circle cx="3.5" cy="8" r="1.25" />
          <circle cx="8" cy="8" r="1.25" />
          <circle cx="12.5" cy="8" r="1.25" />
        </svg>
      </Button>
    {/if}
  </li>
{/snippet}

<ContextMenu menu={rowMenu} onClose={closeRowMenu} />

{#if actions.deleteConfirmSession}
  <ConfirmDialog
    title={t('sessions.delete_confirm_title')}
    body={actions.deleteConfirmMessage}
    confirmLabel={actions.deletePermanently
      ? t('archive.deletePermanently')
      : t('common.delete')}
    onConfirm={actions.confirmDelete}
    onCancel={actions.cancelDelete}
  >
    {#snippet bodyExtra()}
      <ArchiveDeleteOption
        permanent={actions.deletePermanently}
        onChange={(next) => (actions.deletePermanently = next)}
      />
    {/snippet}
  </ConfirmDialog>
{/if}

{#if actions.policySession}
  <Modal
    title={t('sessions.compactionPolicy')}
    labelledById="session-compaction-policy-title"
    closeDisabled={actions.policySaving}
    onClose={actions.closePolicyEdit}
  >
    {#snippet body()}
      <div class="modal-body session-policy-modal__body">
        <div class="session-policy-modal__inheritance">
          <div>
            <div class="session-policy-modal__label">
              {t('sessions.compactionOverride')}
            </div>
            <p>
              {t('sessions.compactionOverrideDescription')}
            </p>
          </div>
          <Toggle
            checked={actions.policyUsesOverride}
            disabled={actions.policySaving}
            ariaLabel={t('sessions.compactionOverride')}
            onChange={(enabled) => (actions.policyUsesOverride = enabled)}
          />
        </div>
        <CompactionPolicyEditor
          value={actions.policyDraft}
          disabled={!actions.policyUsesOverride || actions.policySaving}
          idPrefix="session-compaction-policy"
          onChange={(next) => (actions.policyDraft = next)}
        />
        {#if actions.policyError}
          <p class="session-policy-modal__error" role="alert">
            {actions.policyError}
          </p>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={actions.policySaving}
        onClick={actions.closePolicyEdit}
      >
        {t('common.cancel')}
      </Button>
      <Button disabled={actions.policySaving} onClick={actions.savePolicy}>
        {actions.policySaving ? t('common.saving') : t('common.save')}
      </Button>
    {/snippet}
  </Modal>
{/if}
