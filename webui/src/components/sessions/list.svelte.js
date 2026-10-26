import { onDestroy, untrack } from 'svelte';
import { SvelteMap, SvelteSet } from 'svelte/reactivity';
import { asText } from './presentation.js';
import {
  applyProjectGroups,
  appendProjectGroupPage,
  applySessionList,
  appendSessionList,
  createSessionListState,
  overlayLiveSessionActivity,
  selectSession,
  visibleSessionsForSelection,
} from '$lib/sessionListView.js';
import {
  listProjectSessions,
  listSessionProjectGroups,
  listSessions,
} from '$lib/api.js';
import {
  createCoalescedRefresh,
  sessionInvalidationListsTarget,
  takeSessionInvalidations,
} from '$lib/sessionInvalidation.js';

const SESSION_INITIAL_DISPLAY_LIMIT = 35;
const SESSION_DISPLAY_INCREMENT = 20;
// The newest Sessions a Project group shows, and how many more each
// "Show more" adds.
export const PROJECT_GROUP_PAGE_SIZE = 5;

// Owns authoritative Session-list snapshots, selection and pagination for
// both list modes. Source changes and refreshes share one request generation.
export function createSessionList(context) {
  let sessionState = $state(createSessionListState());
  let sessionsWithLiveActivity = $derived(
    overlayLiveSessionActivity(
      sessionState.sessions,
      context.liveActivity,
      context.agentId,
      context.sessionActivity,
    ),
  );
  let visibleSessions = $derived(
    visibleSessionsForSelection(sessionsWithLiveActivity, {
      filters: context.filters,
      selectedSessionId: context.currentSessionId,
    }),
  );
  let nextCursor = $state(null);
  let totalSessionCount = $state(0);
  let loadingMore = $state(false);
  let hasMoreToDisplay = $derived(nextCursor !== null);
  let remainingSessionCount = $derived(
    Math.max(totalSessionCount - visibleSessions.length, 0),
  );
  // The by-Project view: the groups of `session.project_groups`.
  let projectState = $state({ groups: [], loading: false, error: null });
  let loadingMoreGroupKey = $state(null);

  let loadedListKey = '';
  let loadVersion = 0;

  // The list reloads when the addressed agent changes, when the All-agents
  // filter changes which addresses are loaded, or when the roster itself
  // changes while that filter is on.
  let listSourceKey = $derived(
    [
      context.filters.byProject ? '~projects' : asText(context.agentId),
      context.filters.allAgents && !context.filters.byProject
        ? context.rosterAgents.map((entry) => entry.address).join('|')
        : '',
      Number(context.filters.channels),
      Number(context.filters.subagents),
      Number(context.filters.memoryReflections),
      Number(context.filters.skillReflections),
      Number(context.filters.cron),
    ].join('||'),
  );

  $effect(() => {
    if (listSourceKey === loadedListKey) {
      return;
    }

    loadedListKey = listSourceKey;

    if (!context.filters.byProject && !asText(context.agentId)) {
      sessionState = createSessionListState();
      return;
    }

    loadSessions();
  });

  $effect(() => {
    const normalizedCurrentSessionId = asText(context.currentSessionId);

    if (!normalizedCurrentSessionId) {
      if (sessionState.selectedSessionId !== null) {
        sessionState = selectSession(sessionState, null);
      }
      return;
    }

    const hasCurrentSession = sessionState.sessions.some(
      (session) =>
        session.id === normalizedCurrentSessionId &&
        session.agent_address === asText(context.agentId),
    );

    if (!hasCurrentSession) {
      return;
    }

    if (
      sessionState.selectedSessionId === normalizedCurrentSessionId &&
      sessionState.selectedAgentAddress === asText(context.agentId)
    ) {
      return;
    }

    sessionState = selectSession(
      sessionState,
      normalizedCurrentSessionId,
      asText(context.agentId),
    );
  });

  // Reload the list when continuity is uncertain or another window changes a
  // Session of a listed Agent. The viewed conversation stays put — only the
  // list refreshes. A read acknowledgement changes only completion activity,
  // which the live Chat overlay already shows.
  const listReloads = createCoalescedRefresh(() => loadSessions());
  onDestroy(() => listReloads.destroy());
  let lastReloadToken = null;
  $effect(() => {
    if (lastReloadToken === null) {
      lastReloadToken = context.reloadToken;
      return;
    }
    if (context.reloadToken !== lastReloadToken) {
      lastReloadToken = context.reloadToken;
      void listReloads.run();
    }
  });

  let lastInvalidationId = null;
  $effect(() => {
    const entries = context.invalidations;
    untrack(() => {
      const { targets, lastId, overflowed } = takeSessionInvalidations(
        entries,
        lastInvalidationId,
      );
      lastInvalidationId = lastId;
      // The by-Project view lists every Agent.
      if (
        context.filters.byProject &&
        (overflowed || targets.some((target) => !target.readRunId))
      ) {
        listReloads.schedule();
        return;
      }
      const listedAddresses = [
        asText(context.agentId),
        ...(context.filters.allAgents
          ? context.rosterAgents.map((entry) => entry.address)
          : []),
      ];
      if (
        overflowed ||
        targets.some(
          (target) =>
            !target.readRunId &&
            sessionInvalidationListsTarget(target, listedAddresses),
        )
      ) {
        listReloads.schedule();
      }
    });
  });

  // Mutations may finish after selection or filters changed. Reload the
  // drawer's current scope, while each mutation keeps its original target.
  const loadSessions = async () => {
    if (context.filters.byProject) {
      await loadProjectGroups();
      return;
    }
    const targetAgentId = asText(context.agentId);
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
      const normalizedCurrentSessionId = asText(context.currentSessionId);
      if (normalizedCurrentSessionId) {
        sessionState = selectSession(
          sessionState,
          normalizedCurrentSessionId,
          asText(context.agentId),
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
      context.filters.allAgents && context.rosterAgents.length > 0
        ? [
            ...new SvelteSet([
              targetAgentId,
              ...context.rosterAgents.map((entry) => entry.address),
            ]),
          ]
        : targetAgentId;
    const requiredSessionId = asText(context.currentSessionId);
    const result = await listSessions(listedAgents, {
      limit,
      cursor,
      includeSubagents: context.filters.subagents,
      includeMemoryReflections: context.filters.memoryReflections,
      includeSkillReflections: context.filters.skillReflections,
      includeCron: context.filters.cron,
      includeChannels: context.filters.channels === true,
      requiredSession: requiredSessionId
        ? { agentId: asText(context.agentId), sessionId: requiredSessionId }
        : null,
    });
    const namesByAddress = new SvelteMap(
      context.rosterAgents.map((entry) => [entry.address, entry.name]),
    );
    const sessions = (result?.sessions ?? []).map((session) => {
      const owner = asText(session?.agent_address) || targetAgentId;
      return {
        ...session,
        agent_address: owner,
        agent_name: context.filters.allAgents
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
    const targetAgentId = asText(context.agentId);
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

  const categoryQuery = () => ({
    includeSubagents: context.filters.subagents,
    includeMemoryReflections: context.filters.memoryReflections,
    includeSkillReflections: context.filters.skillReflections,
    includeCron: context.filters.cron,
    includeChannels: context.filters.channels === true,
  });

  const loadProjectGroups = async () => {
    const requestVersion = ++loadVersion;
    loadingMoreGroupKey = null;
    projectState = { ...projectState, loading: true, error: null };
    try {
      const result = await listSessionProjectGroups({
        limit: PROJECT_GROUP_PAGE_SIZE,
        ...categoryQuery(),
      });
      if (requestVersion !== loadVersion) {
        return;
      }
      projectState = {
        groups: applyProjectGroups(result),
        loading: false,
        error: null,
      };
    } catch (error) {
      if (requestVersion === loadVersion) {
        projectState = {
          ...projectState,
          loading: false,
          error: error.message,
        };
      }
    }
  };

  const loadMoreInGroup = async (group) => {
    const cursor = group.nextCursor;
    if (cursor === null || loadingMoreGroupKey !== null) {
      return;
    }
    const requestVersion = loadVersion;
    loadingMoreGroupKey = group.key;
    try {
      const page = await listProjectSessions(group.projectId, {
        limit: PROJECT_GROUP_PAGE_SIZE,
        cursor,
        ...categoryQuery(),
      });
      if (requestVersion !== loadVersion) {
        return;
      }
      projectState = {
        ...projectState,
        groups: appendProjectGroupPage(
          projectState.groups,
          group.projectId,
          page,
        ),
      };
    } catch (error) {
      if (requestVersion === loadVersion) {
        projectState = { ...projectState, error: error.message };
      }
    } finally {
      if (requestVersion === loadVersion) {
        loadingMoreGroupKey = null;
      }
    }
  };

  const handleSelectSession = (session) => {
    const owner = session.agent_address || asText(context.agentId);
    sessionState = selectSession(sessionState, session.id, owner);
    // The row's real sub-agent flag decides the footer banner in ChatView:
    // a foreign agent's ordinary session is a normal override view, not a
    // sub-agent session.
    context.onSessionSelected?.(
      session.id,
      owner,
      session.is_subagent_session === true,
    );
  };

  // The by-Project view marks the shown Session; the Agent list keeps its
  // own selection.
  const rowSelected = (session) => {
    const owner = session.agent_address || asText(context.agentId);
    return context.filters.byProject
      ? session.id === asText(context.currentSessionId) &&
          owner === asText(context.agentId)
      : sessionState.selectedSessionId === session.id &&
          sessionState.selectedAgentAddress === owner;
  };

  return {
    get sessionState() {
      return sessionState;
    },
    get sessionsWithLiveActivity() {
      return sessionsWithLiveActivity;
    },
    get visibleSessions() {
      return visibleSessions;
    },
    get loadingMore() {
      return loadingMore;
    },
    get hasMoreToDisplay() {
      return hasMoreToDisplay;
    },
    get remainingSessionCount() {
      return remainingSessionCount;
    },
    get projectState() {
      return projectState;
    },
    get loadingMoreGroupKey() {
      return loadingMoreGroupKey;
    },
    loadSessions,
    loadMoreSessions,
    loadMoreInGroup,
    handleSelectSession,
    rowSelected,
  };
}
