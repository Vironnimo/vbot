<script>
  import { tick } from 'svelte';

  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import Modal from './ui/Modal.svelte';
  import Toggle from './ui/Toggle.svelte';
  import CompactionPolicyEditor from './compaction/CompactionPolicyEditor.svelte';
  import {
    deleteSession,
    listSessions,
    renameSession,
    setSessionCompactionPolicy,
  } from '$lib/api.js';
  import { normalizeCompactionPolicy } from '$lib/compactionPolicy.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    applySessionList,
    createSessionListFilters,
    createSessionListState,
    selectSession,
    sessionDisplayName,
    visibleSessionsForSelection,
  } from '$lib/sessionListView.js';

  let {
    agentId = '',
    currentSessionId = '',
    // Bumped by ChatView on `resource_changed(kind:"sessions")` so a new or
    // switched session created in another window appears here automatically.
    reloadToken = 0,
    // Roster of addressable agents ({ address, name }) the "All agents"
    // filter lists sessions for — the same set the Chat agent bars show.
    agents = [],
    // Called with (sessionId, agentAddress, isSubAgentSession) when a row is
    // picked — the address routes cross-agent selections, the flag drives the
    // sub-agent footer banner in ChatView.
    onSessionSelected = () => {},
    // Called after a successful delete with { deletedSessionId, nextSessionId,
    // agentAddress } so ChatView can navigate if it was viewing the removed
    // session (#2).
    onSessionDeleted = () => {},
  } = $props();

  const timestampFormatter = new Intl.DateTimeFormat(activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: 'short',
  });

  let sessionState = $state(createSessionListState());
  let filters = $state(createSessionListFilters());
  let visibleSessions = $derived(
    visibleSessionsForSelection(sessionState.sessions, {
      filters,
      selectedSessionId: currentSessionId,
    }),
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

  // Row-action state: which row's "…" menu is open, which row is being renamed
  // inline, the draft title, and any rename error. Only ever one of each at a
  // time — opening a menu or starting an edit on another row supersedes.
  let openMenuSessionId = $state(null);
  let menuTriggerElement = $state(null);
  let menuElement = $state(null);
  let menuStyle = $state('visibility: hidden;');
  let menuPlacement = $state('bottom');
  let editingSessionId = $state(null);
  let editingAgentAddress = $state('');
  let editValue = $state('');
  let renameError = $state(null);
  let renameSaving = $state(false);
  // Filter-dropdown state: the header button's portaled panel, positioned like
  // the row action menu.
  let filterMenuOpen = $state(false);
  let filterMenuTriggerElement = $state(null);
  let filterMenuElement = $state(null);
  let filterMenuStyle = $state('visibility: hidden;');
  let filterMenuPlacement = $state('bottom');
  // Row-delete state: a transient error surfaced when a delete is refused (for
  // example a busy session, #4) and an in-flight guard against double-clicks.
  let actionError = $state(null);
  let deleting = $state(false);
  // The session awaiting delete confirmation (null = dialog closed). The delete
  // only runs once the confirm dialog resolves.
  let deleteConfirmSession = $state(null);
  let policySession = $state(null);
  let policyUsesOverride = $state(false);
  let policyDraft = $state(null);
  let policySaving = $state(false);
  let policyError = $state(null);

  const SESSION_TITLE_MAX_LENGTH = 200;
  const SESSION_ACTION_MENU_FALLBACK_WIDTH = 160;
  const SESSION_FILTER_MENU_WIDTH = 230;
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
      (session) => session.id === normalizedCurrentSessionId,
    );

    if (!hasCurrentSession) {
      return;
    }

    if (sessionState.selectedSessionId === normalizedCurrentSessionId) {
      return;
    }

    sessionState = selectSession(sessionState, normalizedCurrentSessionId);
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
    sessionState = {
      ...sessionState,
      loading: true,
      error: null,
    };

    try {
      const rawSessions = await loadRawSessions(targetAgentId);
      if (requestVersion !== loadVersion) {
        return;
      }

      sessionState = applySessionList(sessionState, rawSessions);
      const normalizedCurrentSessionId = asText(currentSessionId);
      if (normalizedCurrentSessionId) {
        sessionState = selectSession(sessionState, normalizedCurrentSessionId);
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

  // One session.list call for the selected agent, or one per roster address
  // when the All-agents filter is on. Each merged row is tagged with its
  // owning address and display name so rows stay attributable and actions
  // address the right agent.
  const loadRawSessions = async (targetAgentId) => {
    if (!filters.allAgents || rosterAgents.length === 0) {
      const result = await listSessions(targetAgentId);
      return result?.sessions ?? [];
    }

    const results = await Promise.allSettled(
      rosterAgents.map((entry) => listSessions(entry.address)),
    );
    const merged = [];
    let firstError = null;
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        const entry = rosterAgents[index];
        for (const session of result.value?.sessions ?? []) {
          merged.push({
            ...session,
            agent_address: entry.address,
            agent_name: entry.name,
          });
        }
      } else if (firstError === null) {
        firstError = result.reason;
      }
    });
    if (merged.length === 0 && firstError !== null) {
      throw firstError;
    }
    return merged;
  };

  const handleSelectSession = (session) => {
    sessionState = selectSession(sessionState, session.id);
    // The row's real sub-agent flag decides the footer banner in ChatView:
    // a foreign agent's ordinary session is a normal override view, not a
    // sub-agent session.
    onSessionSelected?.(
      session.id,
      session.agent_address || asText(agentId),
      session.is_subagent_session === true,
    );
  };

  // -- filter dropdown -------------------------------------------------------

  const toggleFilterMenu = async (triggerElement) => {
    if (filterMenuOpen) {
      closeFilterMenu();
      return;
    }

    closeMenu();
    filterMenuOpen = true;
    filterMenuTriggerElement = triggerElement;
    filterMenuStyle = 'visibility: hidden;';
    await tick();
    updateFilterMenuPosition();
  };

  const closeFilterMenu = () => {
    filterMenuOpen = false;
    filterMenuTriggerElement = null;
    filterMenuElement = null;
    filterMenuStyle = 'visibility: hidden;';
    filterMenuPlacement = 'bottom';
  };

  const updateFilterMenuPosition = () => {
    if (!filterMenuOpen || !filterMenuTriggerElement || !filterMenuElement) {
      return;
    }

    const panelRect = filterMenuElement.getBoundingClientRect();
    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(filterMenuTriggerElement, {
        contentHeight: filterMenuElement.scrollHeight || panelRect.height,
        panelWidth: panelRect.width || SESSION_FILTER_MENU_WIDTH,
        horizontalAlign: 'end',
      });

    filterMenuPlacement = placement;
    filterMenuStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  };

  const setFilter = (key, checked) => {
    filters = { ...filters, [key]: checked };
  };

  const toggleMenu = async (sessionId, triggerElement) => {
    if (openMenuSessionId === sessionId) {
      closeMenu();
      return;
    }

    openMenuSessionId = sessionId;
    menuTriggerElement = triggerElement;
    menuStyle = 'visibility: hidden;';
    await tick();
    updateMenuPosition();
  };

  const closeMenu = () => {
    openMenuSessionId = null;
    menuTriggerElement = null;
    menuElement = null;
    menuStyle = 'visibility: hidden;';
    menuPlacement = 'bottom';
  };

  const updateMenuPosition = () => {
    if (openMenuSessionId === null || !menuTriggerElement || !menuElement) {
      return;
    }

    const menuRect = menuElement.getBoundingClientRect();
    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(menuTriggerElement, {
        contentHeight: menuElement.scrollHeight || menuRect.height,
        panelWidth: menuRect.width || SESSION_ACTION_MENU_FALLBACK_WIDTH,
        horizontalAlign: 'end',
      });

    menuPlacement = placement;
    menuStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  };

  // Enter inline-rename for a row. Seeds the field with the existing custom
  // title (empty when the row currently shows an automatic label, so the user
  // names it fresh).
  const startRename = (session) => {
    closeMenu();
    editingSessionId = session.id;
    editingAgentAddress = session.agent_address || asText(agentId);
    editValue = session.title ?? '';
    renameError = null;
  };

  const cancelRename = () => {
    editingSessionId = null;
    editingAgentAddress = '';
    editValue = '';
    renameError = null;
  };

  const submitRename = async () => {
    const sessionId = editingSessionId;
    const targetAgentId = editingAgentAddress || asText(agentId);
    if (!sessionId || !targetAgentId || renameSaving) {
      return;
    }

    renameSaving = true;
    renameError = null;
    try {
      await renameSession(targetAgentId, sessionId, editValue);
      editingSessionId = null;
      editingAgentAddress = '';
      editValue = '';
      // Re-fetch so the row reflects the server-normalized title (and the
      // fallback label when the name was cleared).
      await loadSessions(targetAgentId);
    } catch (error) {
      renameError =
        error.message ||
        t('sessions.rename_error', 'The session could not be renamed.');
    } finally {
      renameSaving = false;
    }
  };

  // Delete (archive) a session from the row menu. The shared ConfirmDialog
  // guards the click (#3); for a channel-bound session the body also notes it
  // will resume empty on the next inbound message (#5a). The server returns
  // where to land, which ChatView uses to navigate if it was viewing the
  // removed session.
  const requestDelete = (session) => {
    closeMenu();
    const targetAgentId = asText(agentId);
    if (!targetAgentId || deleting) {
      return;
    }
    deleteConfirmSession = session;
  };

  const startPolicyEdit = (session) => {
    closeMenu();
    policySession = session;
    policyUsesOverride = Boolean(session.compaction_policy_override);
    policyDraft = normalizeCompactionPolicy(
      session.compaction_policy_override ?? session.compaction_policy_effective,
    );
    policyError = null;
  };

  const closePolicyEdit = () => {
    if (policySaving) return;
    policySession = null;
    policyDraft = null;
    policyError = null;
  };

  const savePolicy = async () => {
    const targetAgentId = policySession?.agent_address || asText(agentId);
    if (!targetAgentId || !policySession || policySaving) return;
    policySaving = true;
    policyError = null;
    try {
      await setSessionCompactionPolicy(
        targetAgentId,
        policySession.id,
        policyUsesOverride ? normalizeCompactionPolicy(policyDraft) : null,
      );
      policySession = null;
      policyDraft = null;
      await loadSessions(targetAgentId);
    } catch (error) {
      policyError =
        error.message ||
        t(
          'sessions.compactionSaveError',
          'The Compaction Policy could not be saved.',
        );
    } finally {
      policySaving = false;
    }
  };

  // The confirm body reflects whether the pending session is channel-bound.
  let deleteConfirmMessage = $derived.by(() => {
    const session = deleteConfirmSession;
    if (!session) {
      return '';
    }
    const name = session.display_name || sessionDisplayName(session);
    return session.is_channel_session
      ? t(
          'sessions.delete_confirm_channel',
          'Delete session "{name}"? It is archived and can be restored. The channel ' +
            'conversation will start fresh on the next incoming message.',
          { name },
        )
      : t(
          'sessions.delete_confirm',
          'Delete session "{name}"? It is archived and can be restored.',
          { name },
        );
  });

  const cancelDelete = () => {
    deleteConfirmSession = null;
  };

  const confirmDelete = async () => {
    const session = deleteConfirmSession;
    deleteConfirmSession = null;
    const targetAgentId = session?.agent_address || asText(agentId);
    if (!session || !targetAgentId || deleting) {
      return;
    }

    deleting = true;
    actionError = null;
    try {
      const result = await deleteSession(targetAgentId, session.id);
      onSessionDeleted?.({
        deletedSessionId: session.id,
        nextSessionId: asText(result?.next_session_id),
        agentAddress: targetAgentId,
      });
      // Re-fetch so the deleted row disappears immediately, without waiting for
      // the resource_changed round-trip.
      await loadSessions(targetAgentId);
    } catch (error) {
      actionError =
        error.message ||
        t('sessions.delete_error', 'The session could not be deleted.');
    } finally {
      deleting = false;
    }
  };

  const handleRenameKeydown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitRename();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      cancelRename();
    }
  };

  // Close an open row menu or the filter dropdown on an outside click or
  // Escape, mirroring the Dropdown primitive. Both panels are portaled, so
  // their original trigger areas and document-root panels must count as
  // inside.
  const handleDocumentMouseDown = (event) => {
    if (
      event.target instanceof Element &&
      ((filterMenuOpen &&
        (event.target.closest('.session-drawer__filter') ||
          filterMenuElement?.contains(event.target))) ||
        (openMenuSessionId !== null &&
          (event.target.closest('.session-row__actions') ||
            menuElement?.contains(event.target))))
    ) {
      return;
    }
    closeMenu();
    closeFilterMenu();
  };

  const handleDocumentKeyDown = (event) => {
    if (event.key === 'Escape') {
      closeMenu();
      closeFilterMenu();
    }
  };

  const handleWindowScroll = (event) => {
    if (
      event.target instanceof Node &&
      (filterMenuElement?.contains(event.target) ||
        menuElement?.contains(event.target))
    ) {
      return;
    }
    closeMenu();
    closeFilterMenu();
  };

  $effect(() => {
    if (openMenuSessionId === null && !filterMenuOpen) {
      return undefined;
    }

    window.addEventListener('scroll', handleWindowScroll, true);
    return () => {
      window.removeEventListener('scroll', handleWindowScroll, true);
    };
  });

  // Focus (and select) the inline rename field as soon as it mounts.
  const autofocusRename = (node) => {
    node.focus();
    node.select();
  };

  const formatTimestamp = (value) => {
    const normalizedValue = asText(value);
    if (!normalizedValue) {
      return t('common.unknown', 'Unknown');
    }

    const parsedValue = Date.parse(normalizedValue);
    if (Number.isNaN(parsedValue)) {
      return normalizedValue;
    }

    return timestampFormatter.format(new Date(parsedValue));
  };

  const sessionHoverDetails = (session) => {
    const lines = [session.display_name || sessionDisplayName(session)];

    if (session.agent_name) {
      lines.push(`${t('sessions.agent', 'Agent')}: ${session.agent_name}`);
    }

    lines.push(
      `${t('sessions.last_active', 'Last active')}: ${formatTimestamp(session.last_active_at ?? session.created_at)}`,
    );

    if (session.source_channel_id) {
      lines.push(
        `${t('sessions.source_channel', 'Source channel')}: ${session.source_channel_id}`,
      );
    }

    if (session.subagent_parent) {
      lines.push(
        `${t('sessions.subagent_parent', 'Parent')}: ${session.subagent_parent.agent_id}/${session.subagent_parent.session_id}`,
      );
    }

    return lines.join('\n');
  };

  const resolvePlatformLabel = (platform) => {
    if (platform === 'telegram') {
      return t('sessions.platform_telegram', 'Telegram');
    }
    if (platform === 'discord') {
      return t('sessions.platform_discord', 'Discord');
    }
    const normalizedPlatform = asText(platform);
    if (!normalizedPlatform) {
      return t('sessions.platform_channel', 'Channel');
    }
    return `${normalizedPlatform.slice(0, 1).toUpperCase()}${normalizedPlatform.slice(1)}`;
  };

  const REFLECTION_BADGE_RUN_KINDS = [
    'memory_reflection',
    'skill_reflection',
    'reflection',
  ];

  function reflectionBadgeKinds(session) {
    return REFLECTION_BADGE_RUN_KINDS.filter((runKind) =>
      session.run_kinds.includes(runKind),
    );
  }

  function asText(value) {
    if (value === null || value === undefined) {
      return '';
    }
    const normalizedValue = String(value).trim();
    return normalizedValue;
  }
</script>

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

<svelte:window onresize={closeMenu} />

<aside class="session-drawer" aria-label={t('sessions.title', 'Sessions')}>
  <div class="session-drawer__header">
    <h3 class="session-drawer__title">{t('sessions.title', 'Sessions')}</h3>
    <div class="session-drawer__filter">
      <button
        type="button"
        class="session-drawer__filter-trigger"
        class:session-drawer__filter-trigger--active={activeFilterCount > 0}
        class:session-drawer__filter-trigger--open={filterMenuOpen}
        aria-label={t('sessions.filtersAria', 'Session list filters')}
        aria-haspopup="menu"
        aria-expanded={filterMenuOpen}
        onclick={(event) => toggleFilterMenu(event.currentTarget)}
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
      {#if filterMenuOpen}
        <div
          bind:this={filterMenuElement}
          use:portal
          class="session-drawer__filter-menu"
          role="menu"
          data-placement={filterMenuPlacement}
          data-positioning="fixed"
          style={filterMenuStyle}
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

  {#if actionError}
    <p class="session-drawer__state session-drawer__state--error" role="alert">
      {actionError}
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
    <ul class="session-drawer__list">
      {#each visibleSessions as session (session.id)}
        <li
          class="session-row"
          class:session-row--editing={editingSessionId === session.id}
        >
          {#if editingSessionId === session.id}
            <div class="session-row__edit">
              <input
                class="session-row__edit-input"
                type="text"
                value={editValue}
                maxlength={SESSION_TITLE_MAX_LENGTH}
                placeholder={t('sessions.rename_placeholder', 'Session name')}
                aria-label={t('sessions.rename_label', 'Rename session')}
                disabled={renameSaving}
                oninput={(event) => (editValue = event.currentTarget.value)}
                onkeydown={handleRenameKeydown}
                use:autofocusRename
              />
              {#if renameError}
                <p class="session-row__edit-error" role="alert">
                  {renameError}
                </p>
              {/if}
            </div>
          {:else}
            <button
              type="button"
              class:session-row__select--active={sessionState.selectedSessionId ===
                session.id}
              class="session-row__select"
              onclick={() => handleSelectSession(session)}
              use:tooltip={sessionHoverDetails(session)}
            >
              <div class="session-row__heading">
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
                class:session-row__menu-trigger--open={openMenuSessionId ===
                  session.id}
                aria-label={t('sessions.actions', 'Session actions')}
                aria-haspopup="menu"
                aria-expanded={openMenuSessionId === session.id}
                onclick={(event) => toggleMenu(session.id, event.currentTarget)}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <circle cx="8" cy="3" r="1.4" />
                  <circle cx="8" cy="8" r="1.4" />
                  <circle cx="8" cy="13" r="1.4" />
                </svg>
              </button>
              {#if openMenuSessionId === session.id}
                <div
                  bind:this={menuElement}
                  use:portal
                  class="session-row__menu"
                  role="menu"
                  data-placement={menuPlacement}
                  data-positioning="fixed"
                  style={menuStyle}
                >
                  <button
                    type="button"
                    class="session-row__menu-item"
                    role="menuitem"
                    onclick={() => startRename(session)}
                  >
                    {t('sessions.rename', 'Rename')}
                  </button>
                  <button
                    type="button"
                    class="session-row__menu-item"
                    role="menuitem"
                    onclick={() => startPolicyEdit(session)}
                  >
                    {t('sessions.compactionPolicy', 'Compaction Policy')}
                  </button>
                  <button
                    type="button"
                    class="session-row__menu-item session-row__menu-item--danger"
                    role="menuitem"
                    onclick={() => requestDelete(session)}
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
  {/if}
</aside>

{#if deleteConfirmSession}
  <ConfirmDialog
    title={t('sessions.delete_confirm_title', 'Delete session')}
    body={deleteConfirmMessage}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDelete}
    onCancel={cancelDelete}
  />
{/if}

{#if policySession}
  <Modal
    title={t('sessions.compactionPolicy', 'Compaction Policy')}
    labelledById="session-compaction-policy-title"
    closeDisabled={policySaving}
    onClose={closePolicyEdit}
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
            checked={policyUsesOverride}
            disabled={policySaving}
            ariaLabel={t('sessions.compactionOverride', 'Session override')}
            onChange={(enabled) => (policyUsesOverride = enabled)}
          />
        </div>
        <CompactionPolicyEditor
          value={policyDraft}
          disabled={!policyUsesOverride || policySaving}
          idPrefix="session-compaction-policy"
          onChange={(next) => (policyDraft = next)}
        />
        {#if policyError}
          <p class="session-policy-modal__error" role="alert">{policyError}</p>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={policySaving}
        onClick={closePolicyEdit}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button disabled={policySaving} onClick={savePolicy}>
        {policySaving
          ? t('common.saving', 'Saving…')
          : t('common.save', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

<style>
  .session-drawer {
    display: flex;
    width: 295px;
    min-width: 295px;
    flex-direction: column;
    border-right: 1px solid var(--border);
    background: var(--surface);
    overflow: hidden;
  }

  :global(.session-policy-modal__body) {
    display: grid;
    gap: 18px;
    min-width: min(620px, 78vw);
  }

  .session-policy-modal__inheritance {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
  }

  .session-policy-modal__label {
    color: var(--text-hi);
    font: 500 13px var(--font-ui);
  }

  .session-policy-modal__inheritance p,
  .session-policy-modal__error {
    margin: 3px 0 0;
    color: var(--text-med);
    font: 12px/1.45 var(--font-ui);
  }

  .session-policy-modal__error {
    color: var(--danger);
  }

  .session-drawer__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  .session-drawer__title {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .session-drawer__filter {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    white-space: nowrap;
  }

  .session-drawer__filter-trigger {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 26px;
    padding: 0;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    cursor: pointer;
    transition:
      background 150ms ease,
      color 150ms ease,
      border-color 150ms ease;
  }

  .session-drawer__filter-trigger:hover,
  .session-drawer__filter-trigger:focus-visible,
  .session-drawer__filter-trigger--open {
    outline: none;
    background: var(--accent-12);
    color: var(--text-hi);
    border-color: var(--accent-40);
  }

  .session-drawer__filter-trigger--active {
    color: var(--accent);
    border-color: var(--accent-40);
  }

  .session-drawer__filter-trigger svg {
    width: 14px;
    height: 14px;
  }

  .session-drawer__filter-count {
    position: absolute;
    top: -5px;
    right: -5px;
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 13px;
    height: 13px;
    padding: 0 3px;
    border-radius: 7px;
    background: var(--accent);
    color: var(--surface);
    font-family: var(--font-ui);
    font-size: 9px;
    font-weight: 600;
    line-height: 1;
  }

  .session-drawer__filter-menu {
    position: fixed;
    z-index: var(--z-floating);
    width: max-content;
    min-width: 230px;
    max-width: calc(100vw - 16px);
    overflow-y: auto;
    padding: 6px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-3);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }

  .session-drawer__filter-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 7px 9px;
    border-radius: var(--r-sm);
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
  }

  .session-drawer__filter-row:hover {
    background: var(--accent-08);
  }

  .session-row__agent {
    margin-top: 2px;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: var(--fs-label-sm);
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-drawer__list {
    margin: 0;
    padding: 12px 10px 14px;
    list-style: none;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .session-row {
    position: relative;
    flex: 0 0 auto;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
    box-shadow: inset 3px 0 0 transparent;
    transition:
      border-color 150ms ease,
      box-shadow 150ms ease,
      background 150ms ease;
  }

  .session-row:has(.session-row__select--active) {
    border-color: var(--accent-40);
    box-shadow: inset 3px 0 0 var(--accent);
  }

  .session-row__select {
    width: 100%;
    min-height: 48px;
    border: 0;
    border-radius: inherit;
    padding: 10px 36px 10px 14px;
    text-align: left;
    background: transparent;
    color: var(--text-hi);
    display: flex;
    flex-direction: column;
    justify-content: center;
    line-height: 1.35;
    transition:
      background 150ms ease,
      border-color 150ms ease;
  }

  .session-row__select:hover,
  .session-row__select:focus-visible {
    outline: none;
    background: var(--accent-08);
  }

  .session-row__select--active {
    background: var(--accent-12);
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .session-row__actions {
    position: absolute;
    top: 50%;
    right: 7px;
    transform: translateY(-50%);
  }

  .session-row__menu-trigger {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    opacity: 0.5;
    cursor: pointer;
    transition:
      background 150ms ease,
      color 150ms ease,
      opacity 150ms ease;
  }

  .session-row:hover .session-row__menu-trigger,
  .session-row__menu-trigger:focus-visible,
  .session-row__menu-trigger--open {
    opacity: 1;
  }

  .session-row__menu-trigger:hover,
  .session-row__menu-trigger--open {
    background: var(--surface-3);
    color: var(--text-hi);
  }

  .session-row__menu-trigger:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 1px;
  }

  .session-row__menu-trigger svg {
    width: 15px;
    height: 15px;
    fill: currentColor;
  }

  .session-row__menu {
    position: fixed;
    z-index: var(--z-floating);
    width: max-content;
    min-width: 132px;
    max-width: calc(100vw - 16px);
    overflow-y: auto;
    padding: 4px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-3);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }

  .session-row__menu-item {
    display: block;
    width: 100%;
    padding: 7px 9px;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    text-align: left;
    cursor: pointer;
    transition: background 150ms ease;
  }

  .session-row__menu-item:hover,
  .session-row__menu-item:focus-visible {
    outline: none;
    background: var(--accent-12);
  }

  .session-row__menu-item--danger {
    color: var(--red);
  }

  .session-row__menu-item--danger:hover,
  .session-row__menu-item--danger:focus-visible {
    background: rgba(252, 129, 129, 0.14);
  }

  .session-row__edit {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 11px 12px;
  }

  .session-row__edit-input {
    width: 100%;
    box-sizing: border-box;
    padding: 7px 9px;
    border: 1px solid var(--accent);
    border-radius: var(--r-sm);
    background: var(--surface);
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
  }

  .session-row__edit-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
  }

  .session-row__edit-error {
    margin: 0;
    color: var(--red);
    font-size: 11.5px;
    line-height: 1.35;
  }

  .session-row__heading {
    display: flex;
    width: 100%;
    min-width: 0;
    align-items: center;
    justify-content: flex-start;
    gap: 8px;
  }

  .session-row__name {
    flex: 1 1 110px;
    min-width: 0;
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    font-weight: 600;
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(.session-row__badge) {
    flex-shrink: 0;
    white-space: nowrap;
  }

  .session-row__markers,
  .session-row__marker-anchor {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
  }

  .session-row__markers {
    gap: 4px;
  }

  :global(.badge.session-row__badge--icon) {
    width: 18px;
    height: 18px;
    flex: 0 0 18px;
    justify-content: center;
    gap: 0;
    padding: 0;
    border-radius: var(--r-sm);
  }

  :global(.badge.session-row__badge--icon > svg) {
    flex-shrink: 0;
  }

  .session-row__unread {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    color: var(--blue);
  }

  .session-row__unread-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--blue);
    box-shadow: 0 0 0 2px var(--blue-dim);
  }

  .session-drawer__state {
    margin: 0;
    padding: 10px 12px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }

  .session-drawer__state--error {
    color: var(--red);
  }

  :global(.session-drawer__load-error) {
    margin: 10px 12px;
  }

  :global(.session-drawer__empty-layout) {
    margin: 12px;
  }

  @media (max-width: 640px) {
    .session-drawer {
      width: 100%;
      min-width: 0;
      border-right: 0;
      border-bottom: 1px solid var(--border);
      max-height: 46%;
    }
  }
</style>
