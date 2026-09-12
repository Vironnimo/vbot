<script>
  import { t } from '$lib/i18n.js';
  import { portal } from '$lib/dropdownPanel.js';
  import Toggle from './ui/Toggle.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import {
    asText,
    autofocusRename,
    sessionHoverDetails,
    resolvePlatformLabel,
    reflectionBadgeKinds,
  } from './sessions/presentation.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    sessionDisplayName,
    applySessionList,
    appendSessionList,
    createSessionListFilters,
    createSessionListState,
    overlayLiveSessionActivity,
    selectSession,
    visibleSessionsForSelection,
  } from '$lib/sessionListView.js';
  import Badge from './ui/Badge.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import Modal from './ui/Modal.svelte';
  import CompactionPolicyEditor from './compaction/CompactionPolicyEditor.svelte';
  import { untrack } from 'svelte';
  import { listSessions } from '$lib/api.js';
  import { createSessionActions } from './sessions/actions.svelte.js';
  import { createSessionMenus } from './sessions/menus.svelte.js';
  import './sessions/sessions.css';

  const SESSION_INITIAL_DISPLAY_LIMIT = 35;
  const SESSION_DISPLAY_INCREMENT = 20;

  let {
    agentId = '',
    currentSessionId = '',
    // Bumped by ChatView on `resource_changed(kind:"sessions")` so a new or
    // switched session created in another window appears here automatically.
    reloadToken = 0,
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
    // Called after a successful delete with { deletedSessionId, nextSessionId,
    // agentAddress } so ChatView can navigate if it was viewing the removed
    // session (#2).
    onSessionDeleted = () => {},
  } = $props();
  const menus = createSessionMenus();
  const actions = createSessionActions({
    get agentId() {
      return agentId;
    },
    get closeMenu() {
      return menus.closeMenu;
    },
    get loadSessions() {
      return loadSessions;
    },
    get onSessionDeleted() {
      return onSessionDeleted;
    },
  });

  let sessionState = $state(createSessionListState());
  let filters = $state(
    untrack(() => initialFilters) ?? createSessionListFilters(),
  );
  let sessionsWithLiveActivity = $derived(
    overlayLiveSessionActivity(sessionState.sessions, liveActivity, agentId),
  );
  let visibleSessions = $derived(
    visibleSessionsForSelection(sessionsWithLiveActivity, {
      filters,
      selectedSessionId: currentSessionId,
    }),
  );
  let displayedSessions = $derived(visibleSessions);
  let nextCursor = $state(null);
  let totalSessionCount = $state(0);
  let loadingMore = $state(false);
  let hasMoreToDisplay = $derived(nextCursor !== null);
  let remainingSessionCount = $derived(
    Math.max(totalSessionCount - visibleSessions.length, 0),
  );
  let activeFilterCount = $derived(
    Number(filters.allAgents) +
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

  const SESSION_FILTER_ROWS = [
    {
      key: 'allAgents',
      labelKey: 'sessions.filters.allAgents',
      labelFallback: 'All agents',
    },
    {
      key: 'subagents',
      labelKey: 'sessions.filters.subagents',
      labelFallback: 'Subagent runs',
    },
    {
      key: 'memoryReflections',
      labelKey: 'sessions.filters.memoryReflections',
      labelFallback: 'Memory reflections',
    },
    {
      key: 'skillReflections',
      labelKey: 'sessions.filters.skillReflections',
      labelFallback: 'Skill reflections',
    },
    {
      key: 'cron',
      labelKey: 'sessions.filters.cron',
      labelFallback: 'Cron runs',
    },
  ];

  let loadedListKey = '';
  let loadVersion = 0;

  // The list reloads when the addressed agent changes, when the All-agents
  // filter changes which addresses are loaded, or when the roster itself
  // changes while that filter is on.
  let listSourceKey = $derived(
    [
      asText(agentId),
      filters.allAgents
        ? rosterAgents.map((entry) => entry.address).join('|')
        : '',
      Number(filters.subagents),
      Number(filters.memoryReflections),
      Number(filters.skillReflections),
      Number(filters.cron),
    ].join('||'),
  );

  $effect(() => {
    if (listSourceKey === loadedListKey) {
      return;
    }

    loadedListKey = listSourceKey;

    if (!asText(agentId)) {
      sessionState = createSessionListState();
      return;
    }

    loadSessions();
  });

  $effect(() => {
    const normalizedCurrentSessionId = asText(currentSessionId);

    if (!normalizedCurrentSessionId) {
      return;
    }

    const hasCurrentSession = sessionState.sessions.some(
      (session) =>
        session.id === normalizedCurrentSessionId &&
        session.agent_address === asText(agentId),
    );

    if (!hasCurrentSession) {
      return;
    }

    if (
      sessionState.selectedSessionId === normalizedCurrentSessionId &&
      sessionState.selectedAgentAddress === asText(agentId)
    ) {
      return;
    }

    sessionState = selectSession(
      sessionState,
      normalizedCurrentSessionId,
      asText(agentId),
    );
  });

  // Reload the list when another window creates/switches a session
  // (`resource_changed(kind:"sessions")`, forwarded by ChatView). The viewed
  // conversation stays put — only the list refreshes.
  let lastReloadToken = null;
  $effect(() => {
    if (lastReloadToken === null) {
      lastReloadToken = reloadToken;
      return;
    }
    if (reloadToken !== lastReloadToken) {
      lastReloadToken = reloadToken;
      loadSessions();
    }
  });

  const loadSessions = async (targetAgentId = asText(agentId)) => {
    if (!targetAgentId) {
      sessionState = createSessionListState();
      return;
    }

    const requestVersion = ++loadVersion;
    nextCursor = null;
    totalSessionCount = 0;
    loadingMore = false;
    sessionState = {
      ...sessionState,
      loading: true,
      error: null,
    };

    try {
      const result = await loadRawSessions(
        targetAgentId,
        SESSION_INITIAL_DISPLAY_LIMIT,
      );
      if (requestVersion !== loadVersion) {
        return;
      }

      sessionState = applySessionList(sessionState, result.sessions);
      nextCursor = result.nextCursor;
      totalSessionCount = result.totalCount;
      const normalizedCurrentSessionId = asText(currentSessionId);
      if (normalizedCurrentSessionId) {
        sessionState = selectSession(
          sessionState,
          normalizedCurrentSessionId,
          asText(agentId),
        );
      }
    } catch (error) {
      if (requestVersion !== loadVersion) {
        return;
      }

      sessionState = {
        ...sessionState,
        loading: false,
        error: error.message,
      };
    }
  };

  // The server applies filtering, global ordering, and keyset pagination in
  // SQLite for either one owner or the complete roster. The browser keeps only
  // the pages the user has actually reached.
  const loadRawSessions = async (targetAgentId, limit, cursor = null) => {
    const listedAgents =
      filters.allAgents && rosterAgents.length > 0
        ? [
            ...new Set([
              targetAgentId,
              ...rosterAgents.map((entry) => entry.address),
            ]),
          ]
        : targetAgentId;
    const requiredSessionId = asText(currentSessionId);
    const result = await listSessions(listedAgents, {
      limit,
      cursor,
      includeSubagents: filters.subagents,
      includeMemoryReflections: filters.memoryReflections,
      includeSkillReflections: filters.skillReflections,
      includeCron: filters.cron,
      requiredSession: requiredSessionId
        ? { agentId: asText(agentId), sessionId: requiredSessionId }
        : null,
    });
    const namesByAddress = new Map(
      rosterAgents.map((entry) => [entry.address, entry.name]),
    );
    const sessions = (result?.sessions ?? []).map((session) => {
      const owner = asText(session?.agent_address) || targetAgentId;
      return {
        ...session,
        agent_address: owner,
        agent_name: filters.allAgents
          ? namesByAddress.get(owner) || owner
          : null,
      };
    });
    return {
      sessions,
      nextCursor: result?.next_cursor ?? null,
      totalCount: Number.isSafeInteger(result?.total_count)
        ? result.total_count
        : sessions.length,
    };
  };

  const loadMoreSessions = async () => {
    const targetAgentId = asText(agentId);
    const cursor = nextCursor;
    if (!targetAgentId || cursor === null || loadingMore) {
      return;
    }
    const requestVersion = loadVersion;
    loadingMore = true;
    try {
      const result = await loadRawSessions(
        targetAgentId,
        SESSION_DISPLAY_INCREMENT,
        cursor,
      );
      if (requestVersion !== loadVersion || cursor !== nextCursor) {
        return;
      }
      sessionState = appendSessionList(sessionState, result.sessions);
      nextCursor = result.nextCursor;
      totalSessionCount = result.totalCount;
    } catch (error) {
      if (requestVersion === loadVersion) {
        sessionState = { ...sessionState, error: error.message };
      }
    } finally {
      if (requestVersion === loadVersion) {
        loadingMore = false;
      }
    }
  };

  const handleSelectSession = (session) => {
    const owner = session.agent_address || asText(agentId);
    sessionState = selectSession(sessionState, session.id, owner);
    // The row's real sub-agent flag decides the footer banner in ChatView:
    // a foreign agent's ordinary session is a normal override view, not a
    // sub-agent session.
    onSessionSelected?.(
      session.id,
      owner,
      session.is_subagent_session === true,
    );
  };

  const setFilter = (key, checked) => {
    filters = { ...filters, [key]: checked };
    onFiltersChange(filters);
  };

  // Close an open row menu or the filter dropdown on an outside click or
  // Escape, mirroring the Dropdown primitive. Both panels are portaled, so
  // their original trigger areas and document-root panels must count as
  // inside.
  const handleDocumentMouseDown = (event) => {
    if (
      event.target instanceof Element &&
      ((menus.filterMenuOpen &&
        (event.target.closest('.session-drawer__filter') ||
          menus.filterMenuElement?.contains(event.target))) ||
        (menus.openMenuSessionId !== null &&
          (event.target.closest('.session-row__actions') ||
            menus.menuElement?.contains(event.target))))
    ) {
      return;
    }
    menus.closeMenu();
    menus.closeFilterMenu();
  };

  const handleDocumentKeyDown = (event) => {
    if (event.key === 'Escape') {
      menus.closeMenu();
      menus.closeFilterMenu();
    }
  };

  const handleWindowScroll = (event) => {
    if (
      event.target instanceof Node &&
      (menus.filterMenuElement?.contains(event.target) ||
        menus.menuElement?.contains(event.target))
    ) {
      return;
    }
    menus.closeMenu();
    menus.closeFilterMenu();
  };

  $effect(() => {
    if (menus.openMenuSessionId === null && !menus.filterMenuOpen) {
      return undefined;
    }

    window.addEventListener('scroll', handleWindowScroll, true);
    return () => {
      window.removeEventListener('scroll', handleWindowScroll, true);
    };
  });

  const handleListScroll = (event) => {
    if (!hasMoreToDisplay || loadingMore) {
      return;
    }
    const el = event.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 60) {
      loadMoreSessions();
    }
  };

  const sessionRowKey = (session) =>
    `${session.agent_address || asText(agentId)}::${session.id}`;
</script>

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

<svelte:window onresize={menus.closeMenu} />

<aside class="session-drawer" aria-label={t('sessions.title', 'Sessions')}>
  <div class="session-drawer__header">
    <h3 class="session-drawer__title">{t('sessions.title', 'Sessions')}</h3>
    <div class="session-drawer__filter">
      <button
        type="button"
        class="session-drawer__filter-trigger"
        class:session-drawer__filter-trigger--active={activeFilterCount > 0}
        class:session-drawer__filter-trigger--open={menus.filterMenuOpen}
        aria-label={t('sessions.filtersAria', 'Session list filters')}
        aria-haspopup="menu"
        aria-expanded={menus.filterMenuOpen}
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
            aria-label={t('sessions.filtersActive', '{count} active filters', {
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
              <span>{t(filterRow.labelKey, filterRow.labelFallback)}</span>
              <Toggle
                size="sm"
                checked={filters[filterRow.key]}
                ariaLabel={t(filterRow.labelKey, filterRow.labelFallback)}
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

  {#if sessionState.error}
    <Banner variant="error" class="session-drawer__load-error" role="alert">
      <span>{sessionState.error}</span>
      <Button
        variant="secondary"
        disabled={sessionState.loading || !agentId}
        onClick={() => loadSessions()}
      >
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {:else if sessionState.loading && sessionState.sessions.length === 0}
    <p class="session-drawer__state">
      {t('sessions.loading', 'Loading sessions…')}
    </p>
  {:else if sessionState.sessions.length === 0}
    <EmptyState
      density="compact"
      class="session-drawer__empty-layout"
      title={t('chat.sessions.emptyTitle', 'No sessions yet')}
      description={filters.allAgents
        ? t('sessions.no_sessions_all', 'No sessions found.')
        : t('sessions.no_sessions', 'No sessions found for this agent.')}
    />
  {:else if visibleSessions.length === 0}
    <EmptyState
      density="compact"
      class="session-drawer__empty-layout"
      title={t('sessions.noImportantTitle', 'No important sessions')}
      description={t(
        'sessions.noImportantDescription',
        'Use the filters to browse Subagent, Reflection, and Cron sessions.',
      )}
    />
  {:else}
    <ul class="session-drawer__list" onscroll={handleListScroll}>
      {#each displayedSessions as session (sessionRowKey(session))}
        <li
          class="session-row"
          class:session-row--editing={actions.editingSessionId === session.id &&
            actions.editingAgentAddress ===
              (session.agent_address || asText(agentId))}
        >
          {#if actions.editingSessionId === session.id && actions.editingAgentAddress === (session.agent_address || asText(agentId))}
            <div class="session-row__edit">
              <input
                class="session-row__edit-input"
                type="text"
                value={actions.editValue}
                maxlength={actions.SESSION_TITLE_MAX_LENGTH}
                placeholder={t('sessions.rename_placeholder', 'Session name')}
                aria-label={t('sessions.rename_label', 'Rename session')}
                disabled={actions.renameSaving}
                oninput={(event) =>
                  (actions.editValue = event.currentTarget.value)}
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
              class:session-row__select--active={sessionState.selectedSessionId ===
                session.id &&
                sessionState.selectedAgentAddress ===
                  (session.agent_address || asText(agentId))}
              class="session-row__select"
              onclick={() => handleSelectSession(session)}
              use:tooltip={sessionHoverDetails(session)}
            >
              <div class="session-row__heading">
                {#if session.has_active_run}
                  <span
                    class="session-row__active-dot"
                    aria-label={t('sessions.activeRun', 'Active')}
                    use:tooltip={t(
                      'sessions.activeRunHint',
                      'This session is currently running.',
                    )}
                  >
                    <span
                      class="tab-indicator tab-indicator--running"
                      aria-hidden="true"
                    ></span>
                  </span>
                {/if}
                <p class="session-row__name">
                  {session.display_name || sessionDisplayName(session)}
                </p>
                <span class="session-row__markers">
                  {#if session.has_unread_completion && session.id !== currentSessionId}
                    <span
                      class="session-row__unread"
                      aria-label={t('sessions.unreadCompletion', 'Unread')}
                      use:tooltip={t(
                        'sessions.unreadCompletionHint',
                        'This Session has an unread result.',
                      )}
                    >
                      <span
                        class="tab-indicator tab-indicator--unread session-row__unread-dot"
                        aria-hidden="true"
                      ></span>
                    </span>
                  {/if}
                  {#if session.platform}
                    <span
                      class="tooltip-anchor session-row__marker-anchor"
                      use:tooltip={resolvePlatformLabel(session.platform)}
                    >
                      <Badge
                        variant="info"
                        class="session-row__badge session-row__badge--icon"
                        aria-label={resolvePlatformLabel(session.platform)}
                        data-session-marker={`platform-${session.platform}`}
                      >
                        {#if session.platform === 'telegram'}
                          <svg
                            viewBox="0 0 18 18"
                            width="11"
                            height="11"
                            fill="currentColor"
                            aria-hidden="true"
                          >
                            <path
                              d="M15.36 3.27c.39-.15.77.2.67.61l-1.94 9.14c-.07.34-.45.5-.74.31l-3.16-2.13-1.62 1.57c-.22.22-.6.11-.67-.2l-.52-2.41 6.72-5.91c.14-.12-.04-.35-.2-.24L5.6 9.04 2.5 7.8c-.34-.13-.35-.6-.02-.75l12.88-3.78z"
                            />
                          </svg>
                        {:else if session.platform === 'discord'}
                          <svg
                            viewBox="0 0 16 16"
                            width="11"
                            height="11"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.45"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                            aria-hidden="true"
                          >
                            <path
                              d="M4.1 4.1a9.5 9.5 0 0 1 7.8 0c1.15 1.75 1.7 3.7 1.55 5.8a8.8 8.8 0 0 1-2.4 1.25l-.75-1.05"
                            />
                            <path
                              d="M5.7 10.1l-.75 1.05a8.8 8.8 0 0 1-2.4-1.25C2.4 7.8 2.95 5.85 4.1 4.1"
                            />
                            <path d="M5.25 5.05a7.7 7.7 0 0 1 5.5 0" />
                            <circle
                              cx="5.8"
                              cy="7.7"
                              r=".8"
                              fill="currentColor"
                              stroke="none"
                            />
                            <circle
                              cx="10.2"
                              cy="7.7"
                              r=".8"
                              fill="currentColor"
                              stroke="none"
                            />
                          </svg>
                        {:else}
                          <svg
                            viewBox="0 0 14 14"
                            width="11"
                            height="11"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.4"
                            stroke-linecap="round"
                            aria-hidden="true"
                          >
                            <circle
                              cx="7"
                              cy="7"
                              r="1.2"
                              fill="currentColor"
                              stroke="none"
                            />
                            <path
                              d="M4.6 4.6a3.4 3.4 0 0 0 0 4.8M9.4 4.6a3.4 3.4 0 0 1 0 4.8"
                            />
                            <path
                              d="M2.5 2.5a6.35 6.35 0 0 0 0 9M11.5 2.5a6.35 6.35 0 0 1 0 9"
                            />
                          </svg>
                        {/if}
                      </Badge>
                    </span>
                  {/if}
                  {#if session.is_subagent_session}
                    <span
                      class="tooltip-anchor session-row__marker-anchor"
                      use:tooltip={t(
                        'sessions.subagentHint',
                        'A session run by a Subagent working on behalf of a parent session. The parent is shown below.',
                      )}
                    >
                      <Badge
                        variant="neutral"
                        class="session-row__badge session-row__badge--icon"
                        aria-label={t('chat.subagent.label', 'Subagent')}
                        data-session-marker="subagent"
                      >
                        <svg
                          viewBox="0 0 14 14"
                          width="11"
                          height="11"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="1.45"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          aria-hidden="true"
                        >
                          <circle cx="4" cy="3.5" r="1.7" />
                          <circle cx="10.25" cy="9.75" r="1.35" />
                          <path d="M4 5.2v2.55c0 1.1.9 2 2 2h2.9" />
                        </svg>
                      </Badge>
                    </span>
                  {/if}
                  {#if session.is_fork}
                    <span
                      class="tooltip-anchor session-row__marker-anchor"
                      use:tooltip={t(
                        'sessions.forkHint',
                        'A copy of another session. Background reflection and /reflect review a conversation in a fork so the original session stays untouched.',
                      )}
                    >
                      <Badge
                        variant="neutral"
                        class="session-row__badge session-row__badge--icon"
                        aria-label={t('sessions.fork', 'Fork')}
                        data-session-marker="fork"
                      >
                        <svg
                          viewBox="0 0 14 14"
                          width="11"
                          height="11"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="1.45"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          aria-hidden="true"
                        >
                          <circle cx="3.25" cy="3" r="1.25" />
                          <circle cx="3.25" cy="11" r="1.25" />
                          <circle cx="10.5" cy="3.75" r="1.25" />
                          <path
                            d="M3.25 4.25v5.5M4.5 7.25h1.25a3.5 3.5 0 0 0 3.5-3.5"
                          />
                        </svg>
                      </Badge>
                    </span>
                  {/if}
                  {#if session.run_kinds.includes('cron')}
                    <span
                      class="tooltip-anchor session-row__marker-anchor"
                      use:tooltip={t('sessions.runKind.cron', 'Cron')}
                    >
                      <Badge
                        variant="warn"
                        class="session-row__badge session-row__badge--icon"
                        aria-label={t('sessions.runKind.cron', 'Cron')}
                        data-session-marker="cron"
                      >
                        <svg
                          viewBox="0 0 14 14"
                          width="11"
                          height="11"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="1.45"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          aria-hidden="true"
                        >
                          <circle cx="7" cy="7" r="4.75" />
                          <path d="M7 4.25v3.1l2.15 1.2" />
                        </svg>
                      </Badge>
                    </span>
                  {/if}
                  {#each reflectionBadgeKinds(session) as runKind (runKind)}
                    <span
                      class="tooltip-anchor session-row__marker-anchor"
                      use:tooltip={t(`sessions.runKind.${runKind}`, runKind)}
                    >
                      <Badge
                        variant="neutral"
                        class="session-row__badge session-row__badge--icon"
                        aria-label={t(`sessions.runKind.${runKind}`, runKind)}
                        data-session-marker={runKind}
                      >
                        <svg
                          viewBox="0 0 14 14"
                          width="11"
                          height="11"
                          fill="none"
                          stroke="currentColor"
                          stroke-width="1.35"
                          stroke-linecap="round"
                          stroke-linejoin="round"
                          aria-hidden="true"
                        >
                          <path d="M10.9 6.9A4.1 4.1 0 1 1 9.65 4" />
                          <path d="M9.65 1.9V4h-2.1" />
                          <path d="M11.1 1.7v2.2M10 2.8h2.2" />
                        </svg>
                      </Badge>
                    </span>
                  {/each}
                </span>
              </div>
              {#if session.agent_name}
                <span class="session-row__agent">{session.agent_name}</span>
              {/if}
            </button>
            <div class="session-row__actions">
              <button
                type="button"
                class="session-row__menu-trigger"
                class:session-row__menu-trigger--open={menus.openMenuSessionId ===
                  sessionRowKey(session)}
                aria-label={t('sessions.actions', 'Session actions')}
                aria-haspopup="menu"
                aria-expanded={menus.openMenuSessionId ===
                  sessionRowKey(session)}
                onclick={(event) =>
                  menus.toggleMenu(sessionRowKey(session), event.currentTarget)}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <circle cx="8" cy="3" r="1.4" />
                  <circle cx="8" cy="8" r="1.4" />
                  <circle cx="8" cy="13" r="1.4" />
                </svg>
              </button>
              {#if menus.openMenuSessionId === sessionRowKey(session)}
                <div
                  bind:this={menus.menuElement}
                  use:portal
                  class="session-row__menu"
                  role="menu"
                  data-placement={menus.menuPlacement}
                  data-positioning="fixed"
                  style={menus.menuStyle}
                >
                  <button
                    type="button"
                    class="session-row__menu-item"
                    role="menuitem"
                    onclick={() => actions.startRename(session)}
                  >
                    {t('sessions.rename', 'Rename')}
                  </button>
                  <button
                    type="button"
                    class="session-row__menu-item"
                    role="menuitem"
                    onclick={() => actions.startPolicyEdit(session)}
                  >
                    {t('sessions.compactionPolicy', 'Compaction Policy')}
                  </button>
                  <button
                    type="button"
                    class="session-row__menu-item session-row__menu-item--danger"
                    role="menuitem"
                    onclick={() => actions.requestDelete(session)}
                  >
                    {t('sessions.delete', 'Delete')}
                  </button>
                </div>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
    {#if hasMoreToDisplay}
      <p class="session-drawer__more-hint">
        {t('sessions.moreHint', '{count} more sessions — scroll to load', {
          count: remainingSessionCount,
        })}
      </p>
    {/if}
  {/if}
</aside>

{#if actions.deleteConfirmSession}
  <ConfirmDialog
    title={t('sessions.delete_confirm_title', 'Delete session')}
    body={actions.deleteConfirmMessage}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={actions.confirmDelete}
    onCancel={actions.cancelDelete}
  />
{/if}

{#if actions.policySession}
  <Modal
    title={t('sessions.compactionPolicy', 'Compaction Policy')}
    labelledById="session-compaction-policy-title"
    closeDisabled={actions.policySaving}
    onClose={actions.closePolicyEdit}
  >
    {#snippet body()}
      <div class="modal-body session-policy-modal__body">
        <div class="session-policy-modal__inheritance">
          <div>
            <div class="session-policy-modal__label">
              {t('sessions.compactionOverride', 'Session override')}
            </div>
            <p>
              {t(
                'sessions.compactionOverrideDescription',
                'When disabled, this Session follows later Agent or global Policy changes.',
              )}
            </p>
          </div>
          <Toggle
            checked={actions.policyUsesOverride}
            disabled={actions.policySaving}
            ariaLabel={t('sessions.compactionOverride', 'Session override')}
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
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button disabled={actions.policySaving} onClick={actions.savePolicy}>
        {actions.policySaving
          ? t('common.saving', 'Saving…')
          : t('common.save', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}
