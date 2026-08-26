import { asOptionalText, isPlainObject } from './values.js';

const SESSION_FALLBACK_NAME = 'Session';
const BACKGROUND_ONLY_RUN_KINDS = new Set([
  'cron',
  'reflection',
  'memory_reflection',
  'skill_reflection',
]);

// Category toggles that reveal hidden-by-default sessions. `allAgents` is a
// filter-dropdown toggle too, but it changes which sessions are loaded rather
// than how the loaded list is classified, so the view helpers ignore it.
export function createSessionListFilters() {
  return {
    allAgents: false,
    subagents: false,
    memoryReflections: false,
    skillReflections: false,
    cron: false,
  };
}

export function createSessionListState() {
  return {
    sessions: [],
    loading: false,
    error: null,
    selectedSessionId: null,
  };
}

export function applySessionList(state, sessions) {
  const normalizedSessions = normalizeSessions(sessions);
  const currentSelectedSessionId = asOptionalText(state?.selectedSessionId);
  const selectedSessionId =
    currentSelectedSessionId !== null &&
    normalizedSessions.some(
      (session) => session.id === currentSelectedSessionId,
    )
      ? currentSelectedSessionId
      : null;

  return {
    ...(isPlainObject(state) ? state : {}),
    sessions: normalizedSessions,
    loading: false,
    error: null,
    selectedSessionId,
  };
}

export function selectSession(state, sessionId) {
  const normalizedSessionId = asOptionalText(sessionId);
  const sessions = Array.isArray(state?.sessions) ? state.sessions : [];
  const selectedSessionId =
    normalizedSessionId !== null &&
    sessions.some((session) => session.id === normalizedSessionId)
      ? normalizedSessionId
      : null;

  return {
    ...(isPlainObject(state) ? state : {}),
    selectedSessionId,
  };
}

export function sessionDisplayName(session) {
  // A user-set title wins over the automatic first-message title; clearing it
  // reveals that automatic title again, then the channel-derived name / raw id.
  const title = asOptionalText(session?.title);
  if (title !== null) {
    return title;
  }

  const autoTitle = asOptionalText(session?.auto_title);
  if (autoTitle !== null) {
    return autoTitle;
  }

  const platform = asOptionalText(session?.platform);
  const platformConvId = asOptionalText(session?.platform_conv_id);

  if (platform !== null && platformConvId !== null) {
    return `${platform}/${platformConvId}`;
  }

  return asOptionalText(session?.id) ?? SESSION_FALLBACK_NAME;
}

export function visibleSessionsForSelection(
  sessions,
  { filters = null, selectedSessionId = null } = {},
) {
  const normalizedFilters = normalizeFilters(filters);
  const normalizedSelectedSessionId = asOptionalText(selectedSessionId);
  return (Array.isArray(sessions) ? sessions : []).filter(
    (session) =>
      session?.id === normalizedSelectedSessionId ||
      !isSessionHiddenByDefault(session) ||
      isHiddenCategoryEnabled(session, normalizedFilters),
  );
}

export function isSessionHiddenByDefault(session) {
  return isSubAgentSession(session) || isBackgroundOnlySession(session);
}

export function isBackgroundOnlySession(session) {
  const runKinds = normalizeRunKinds(session?.run_kinds);
  const isChannelSession =
    asOptionalText(session?.platform) !== null &&
    asOptionalText(session?.platform_conv_id) !== null;
  return (
    !isChannelSession &&
    runKinds.length > 0 &&
    runKinds.every((runKind) => BACKGROUND_ONLY_RUN_KINDS.has(runKind))
  );
}

// A hidden session becomes visible when every category that hides it is
// enabled. A combined reflection review covers both dimensions, so it matches
// either reflection toggle.
function isHiddenCategoryEnabled(session, filters) {
  if (isSubAgentSession(session)) {
    return filters.subagents;
  }
  if (!isBackgroundOnlySession(session)) {
    return true;
  }
  return (Array.isArray(session?.run_kinds) ? session.run_kinds : []).every(
    (runKind) =>
      (runKind === 'cron' && filters.cron) ||
      (runKind === 'memory_reflection' && filters.memoryReflections) ||
      (runKind === 'skill_reflection' && filters.skillReflections) ||
      (runKind === 'reflection' &&
        (filters.memoryReflections || filters.skillReflections)),
  );
}

function normalizeFilters(filters) {
  if (!isPlainObject(filters)) {
    return createSessionListFilters();
  }
  return {
    allAgents: filters.allAgents === true,
    subagents: filters.subagents === true,
    memoryReflections: filters.memoryReflections === true,
    skillReflections: filters.skillReflections === true,
    cron: filters.cron === true,
  };
}

function normalizeSessions(sessions) {
  const rawSessions = Array.isArray(sessions) ? sessions : [];
  const normalizedSessions = rawSessions
    .map((session) => normalizeSession(session))
    .filter((session) => session !== null);

  normalizedSessions.sort(compareSessions);

  return normalizedSessions;
}

function normalizeSession(session) {
  const id = asOptionalText(session?.id);
  if (id === null) {
    return null;
  }

  const platform = asOptionalText(session?.platform);
  const platformConvId = asOptionalText(session?.platform_conv_id);
  const subagentParent = normalizeSubagentParent(session?.subagent_parent);
  const isSubagentSession =
    session?.is_subagent_session === true || subagentParent !== null;
  const forkSource = isPlainObject(session?.fork_source)
    ? session.fork_source
    : null;
  const runKinds = normalizeRunKinds(session?.run_kinds);

  const normalizedSession = {
    id,
    title: asOptionalText(session?.title),
    auto_title: asOptionalText(session?.auto_title),
    // Owning agent for merged all-agents lists (null in single-agent mode).
    agent_address: asOptionalText(session?.agent_address),
    agent_name: asOptionalText(session?.agent_name),
    created_at: asOptionalText(session?.created_at),
    last_active_at: asOptionalText(session?.last_active_at),
    has_unread_completion: session?.has_unread_completion === true,
    has_active_run: session?.has_active_run === true,
    latest_completion_run_id: asOptionalText(session?.latest_completion_run_id),
    unread_run_id: asOptionalText(session?.unread_run_id),
    unread_run_status: asOptionalText(session?.unread_run_status),
    unread_run_at: asOptionalText(session?.unread_run_at),
    source_channel_id: asOptionalText(session?.source_channel_id),
    platform,
    platform_conv_id: platformConvId,
    is_channel_session: platform !== null && platformConvId !== null,
    is_subagent_session: isSubagentSession,
    subagent_parent: subagentParent,
    fork_source: forkSource,
    is_fork: forkSource !== null,
    run_kinds: runKinds,
    is_background_only: isBackgroundOnlySession({
      run_kinds: runKinds,
      platform,
      platform_conv_id: platformConvId,
    }),
    compaction_policy_override: isPlainObject(
      session?.compaction_policy_override,
    )
      ? session.compaction_policy_override
      : null,
    compaction_policy_effective: isPlainObject(
      session?.compaction_policy_effective,
    )
      ? session.compaction_policy_effective
      : null,
  };

  normalizedSession.display_name = sessionDisplayName(normalizedSession);

  return normalizedSession;
}

function isSubAgentSession(session) {
  return (
    session?.is_subagent_session === true ||
    isPlainObject(session?.subagent_parent)
  );
}

function normalizeRunKinds(runKinds) {
  const normalized = [];
  for (const value of Array.isArray(runKinds) ? runKinds : []) {
    const runKind = asOptionalText(value);
    if (runKind !== null && !normalized.includes(runKind)) {
      normalized.push(runKind);
    }
  }
  return normalized;
}

function normalizeSubagentParent(parent) {
  if (!isPlainObject(parent)) {
    return null;
  }

  const agentId = asOptionalText(parent.agent_id);
  const sessionId = asOptionalText(parent.session_id);
  if (agentId === null || sessionId === null) {
    return null;
  }

  return {
    agent_id: agentId,
    session_id: sessionId,
    // The parent's project anchor (null for an identity parent). A project
    // parent must be addressed as `agent@projekt` on navigation paths.
    project_id: asOptionalText(parent.project_id),
    run_id: asOptionalText(parent.run_id),
    tool_call_id: asOptionalText(parent.tool_call_id),
    tool_call_index: Number.isSafeInteger(parent.tool_call_index)
      ? parent.tool_call_index
      : null,
  };
}

function compareSessions(left, right) {
  const leftTimestamp = resolveTimestamp(left);
  const rightTimestamp = resolveTimestamp(right);

  if (leftTimestamp !== rightTimestamp) {
    return rightTimestamp - leftTimestamp;
  }

  return left.id.localeCompare(right.id);
}

function resolveTimestamp(session) {
  return (
    parseTimestamp(session?.last_active_at) ??
    parseTimestamp(session?.created_at) ??
    0
  );
}

function parseTimestamp(value) {
  if (typeof value !== 'string' || value.length === 0) {
    return null;
  }

  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}
