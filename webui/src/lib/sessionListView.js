import { t } from './i18n.js';
import {
  SESSION_IDLE,
  SESSION_WAITING,
  normalizeActivityState,
  normalizeWaitingFor,
  sessionActivityKey,
} from './sessionActivity.js';
import { asOptionalText, isPlainObject } from './values.js';

// Librarian and Live Sessions are not among them: the server lists them only in
// the scope of their built-in Agent, where they are ordinary conversations.
const BACKGROUND_ONLY_RUN_KINDS = new Set([
  'cron',
  'reflection',
  'memory_reflection',
  'skill_reflection',
]);

const REFLECTION_RUN_KINDS = [
  'reflection',
  'memory_reflection',
  'skill_reflection',
];

// Category toggles that reveal hidden-by-default sessions. `allAgents` is a
// scope toggle; it changes which sessions are loaded rather
// than how the loaded list is classified, so the view helpers ignore it.
// `byProject` shows every Agent's Sessions grouped by the Project they work in
// instead of one Agent's list; `collapsedProjects` names the closed groups by
// `projectGroupKey`.
export function createSessionListFilters() {
  return {
    allAgents: false,
    byProject: false,
    collapsedProjects: [],
    channels: false,
    subagents: false,
    memoryReflections: false,
    skillReflections: false,
    cron: false,
  };
}

export function loadSessionListFilters(area) {
  try {
    const stored = JSON.parse(
      localStorage.getItem(`vbot.chat.sessionFilters.${area}`),
    );
    return isPlainObject(stored) ? normalizeFilters(stored) : null;
  } catch {
    // Missing or unavailable storage leaves the area's default intact.
    return null;
  }
}

export function saveSessionListFilters(area, filters) {
  try {
    localStorage.setItem(
      `vbot.chat.sessionFilters.${area}`,
      JSON.stringify(normalizeFilters(filters)),
    );
  } catch {
    // Storage is optional; filter changes remain usable in memory.
  }
}

export function createSessionListState() {
  return {
    sessions: [],
    loading: false,
    error: null,
    selectedSessionId: null,
    selectedAgentAddress: null,
  };
}

export function applySessionList(state, sessions) {
  const normalizedSessions = normalizeSessions(sessions);
  const currentSelectedSessionId = asOptionalText(state?.selectedSessionId);
  const currentSelectedAgentAddress = asOptionalText(
    state?.selectedAgentAddress,
  );
  const selectedSessionId =
    currentSelectedSessionId !== null &&
    normalizedSessions.some(
      (session) =>
        session.id === currentSelectedSessionId &&
        (currentSelectedAgentAddress === null ||
          session.agent_address === null ||
          session.agent_address === currentSelectedAgentAddress),
    )
      ? currentSelectedSessionId
      : null;

  return {
    ...(isPlainObject(state) ? state : {}),
    sessions: normalizedSessions,
    loading: false,
    error: null,
    selectedSessionId,
    selectedAgentAddress:
      selectedSessionId === null ? null : currentSelectedAgentAddress,
  };
}

export function appendSessionList(state, sessions) {
  const combined = [
    ...(Array.isArray(state?.sessions) ? state.sessions : []),
    ...(Array.isArray(sessions) ? sessions : []),
  ];
  const unique = new Map();
  for (const session of combined) {
    const normalized = normalizeSession(session);
    if (normalized === null) {
      continue;
    }
    unique.set(
      `${normalized.agent_address ?? ''}::${normalized.id}`,
      normalized,
    );
  }
  return applySessionList(state, [...unique.values()]);
}

// A Project group's key: its Project id, '' for the group of Sessions that
// work in no registered Project.
export function projectGroupKey(projectId) {
  return asOptionalText(projectId) ?? '';
}

// The `session.project_groups` groups in server order, each with its
// normalized Sessions, the cursor of its next page and its total count.
export function applyProjectGroups(result) {
  const groups = [];
  for (const group of Array.isArray(result?.groups) ? result.groups : []) {
    if (!isPlainObject(group)) {
      continue;
    }
    const sessions = Array.isArray(group.sessions) ? group.sessions : [];
    groups.push({
      projectId: asOptionalText(group.project_id),
      sessions: normalizeSessions(sessions),
      nextCursor: isPlainObject(group.next_cursor) ? group.next_cursor : null,
      totalCount: Number.isSafeInteger(group.total_count)
        ? group.total_count
        : sessions.length,
    });
  }
  return groups;
}

// Add one more `session.list` page to the group of `projectId`.
export function appendProjectGroupPage(groups, projectId, page) {
  const key = projectGroupKey(projectId);
  return (Array.isArray(groups) ? groups : []).map((group) => {
    if (projectGroupKey(group.projectId) !== key) {
      return group;
    }
    const unique = new Map();
    for (const session of [
      ...group.sessions,
      ...normalizeSessions(page?.sessions),
    ]) {
      unique.set(`${session.agent_address ?? ''}::${session.id}`, session);
    }
    return {
      ...group,
      sessions: normalizeSessions([...unique.values()]),
      nextCursor: isPlainObject(page?.next_cursor) ? page.next_cursor : null,
      totalCount: Number.isSafeInteger(page?.total_count)
        ? page.total_count
        : group.totalCount,
    };
  });
}

// The list rows with Chat's live unread state (`activityRows`) and App's live
// Session activity (`sessionActivity`, see ../sessionActivity.js) laid over
// the last snapshot. Once App holds Session activity, a row it lacks is idle;
// before that, rows keep the activity the server listed. A row whose live
// activity matches keeps its identity.
export function overlayLiveSessionActivity(
  sessions,
  activityRows,
  defaultAgentAddress,
  sessionActivity = null,
) {
  const fallbackAddress = asOptionalText(defaultAgentAddress);
  const activityBySession = new Map();
  for (const activity of Array.isArray(activityRows) ? activityRows : []) {
    const agentAddress = asOptionalText(activity?.agent_address);
    const sessionId = asOptionalText(activity?.session_id);
    if (agentAddress === null || sessionId === null) {
      continue;
    }
    activityBySession.set(`${agentAddress}::${sessionId}`, activity);
  }

  return (Array.isArray(sessions) ? sessions : []).map((session) => {
    const sessionId = asOptionalText(session?.id);
    const agentAddress =
      asOptionalText(session?.agent_address) ?? fallbackAddress;
    if (sessionId === null || agentAddress === null) {
      return session;
    }
    const key = sessionActivityKey(agentAddress, sessionId);
    const activity = activityBySession.get(key);
    const unread = activity
      ? {
          has_unread_completion: activity.has_unread_completion === true,
          latest_completion_run_id: asOptionalText(
            activity.latest_completion_run_id,
          ),
          unread_run_id: asOptionalText(activity.unread_run_id),
          unread_run_status: asOptionalText(activity.unread_run_status),
          unread_run_at: asOptionalText(activity.unread_run_at),
        }
      : null;
    const entry = sessionActivity?.[key];
    const work = sessionActivity
      ? rowActivity(entry?.state, entry?.workStartedAt, entry?.waitingFor)
      : null;
    if (!unread && (!work || sameRowActivity(session, work))) {
      return session;
    }
    return { ...session, ...unread, ...work };
  });
}

// A row's Session activity: `activity_state` ('working', 'waiting' or
// 'idle'), when the work started and what a waiting Session waits for.
function rowActivity(state, workStartedAt, waitingFor) {
  const activityState = normalizeActivityState(state);
  return {
    activity_state: activityState,
    work_started_at:
      activityState === SESSION_IDLE ? null : asOptionalText(workStartedAt),
    waiting_for: normalizeWaitingFor(
      activityState === SESSION_WAITING ? waitingFor : null,
    ),
  };
}

function sameRowActivity(session, work) {
  return (
    session.activity_state === work.activity_state &&
    session.work_started_at === work.work_started_at &&
    Object.keys(work.waiting_for).every(
      (kind) => session.waiting_for?.[kind] === work.waiting_for[kind],
    )
  );
}

export function selectSession(state, sessionId, agentAddress = null) {
  const normalizedSessionId = asOptionalText(sessionId);
  const normalizedAgentAddress = asOptionalText(agentAddress);
  const sessions = Array.isArray(state?.sessions) ? state.sessions : [];
  const selectedSessionId =
    normalizedSessionId !== null &&
    sessions.some(
      (session) =>
        session.id === normalizedSessionId &&
        (normalizedAgentAddress === null ||
          session.agent_address === null ||
          session.agent_address === normalizedAgentAddress),
    )
      ? normalizedSessionId
      : null;

  return {
    ...(isPlainObject(state) ? state : {}),
    selectedSessionId,
    selectedAgentAddress:
      selectedSessionId === null ? null : normalizedAgentAddress,
  };
}

export function sessionDisplayName(session) {
  // A user-set title wins over the automatic first-message title; clearing it
  // reveals that automatic title again, then the channel-derived name. A
  // Session with none of those has no content yet, so it shows the neutral
  // "New Session" label instead of its raw id.
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

  return t('sessions.newSession');
}

// A Reflection Session is the fork a reflection review runs in; it keeps the
// reflection's Run kind even after the user continues it.
export function isReflectionSession(session) {
  const runKinds = Array.isArray(session?.run_kinds) ? session.run_kinds : [];
  return REFLECTION_RUN_KINDS.some((runKind) => runKinds.includes(runKind));
}

export function sessionParentReference(session) {
  const subagentParent = normalizeSubagentParent(session?.subagent_parent);
  if (subagentParent !== null) {
    return {
      kind: 'subagent',
      agent_id: subagentParent.agent_id,
      session_id: subagentParent.session_id,
      project_id: subagentParent.project_id,
    };
  }

  const forkSource = normalizeForkSource(session?.fork_source);
  if (forkSource !== null) {
    return {
      kind: 'fork',
      agent_id: forkSource.agent_id,
      session_id: forkSource.session_id,
      project_id: forkSource.project_id,
    };
  }

  return null;
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
      isReportedBackgroundSession(session) ||
      isHiddenCategoryEnabled(session, normalizedFilters),
  );
}

// A background Run the user was told about (such as a Cron Run that delivers
// to vBot) lists its Session with the conversations, as the server does. It
// stays out of the Agent's landing choice.
function isReportedBackgroundSession(session) {
  return (
    !isSubAgentSession(session) &&
    isBackgroundOnlySession(session) &&
    session?.has_chat_results !== false &&
    session?.has_chat_results !== 0 &&
    asOptionalText(session?.latest_completion_run_id) !== null
  );
}

export function isSessionHiddenByDefault(session) {
  return (
    isChannelSession(session) ||
    isSubAgentSession(session) ||
    isBackgroundOnlySession(session)
  );
}

function isChannelSession(session) {
  return (
    asOptionalText(session?.platform) !== null &&
    asOptionalText(session?.platform_conv_id) !== null
  );
}

export function isBackgroundOnlySession(session) {
  const runKinds = normalizeRunKinds(session?.run_kinds);
  return (
    !isChannelSession(session) &&
    runKinds.length > 0 &&
    runKinds.every(
      (runKind) =>
        BACKGROUND_ONLY_RUN_KINDS.has(runKind) ||
        (runKind === 'system' && session?.has_schedule_runs === true),
    )
  );
}

// A hidden session becomes visible when every category that hides it is
// enabled. A combined reflection review covers both dimensions, so it matches
// either reflection toggle.
function isHiddenCategoryEnabled(session, filters) {
  if (isChannelSession(session) && !filters.channels) {
    return false;
  }
  if (isSubAgentSession(session)) {
    return filters.subagents;
  }
  if (!isBackgroundOnlySession(session)) {
    return true;
  }
  return (Array.isArray(session?.run_kinds) ? session.run_kinds : []).every(
    (runKind) =>
      ((runKind === 'cron' ||
        (runKind === 'system' && session?.has_schedule_runs === true)) &&
        filters.cron) ||
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
    byProject: filters.byProject === true,
    collapsedProjects: [
      ...new Set(
        (Array.isArray(filters.collapsedProjects)
          ? filters.collapsedProjects
          : []
        ).filter((key) => typeof key === 'string'),
      ),
    ],
    channels: filters.channels === true,
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
  const forkSource = normalizeForkSource(session?.fork_source);
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
    ...rowActivity(
      session?.activity_state,
      session?.work_started_at,
      session?.waiting_for,
    ),
    has_chat_results: session?.has_chat_results,
    has_schedule_runs: session?.has_schedule_runs,
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
      has_schedule_runs: session?.has_schedule_runs,
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

function normalizeForkSource(source) {
  if (!isPlainObject(source)) {
    return null;
  }

  const agentId = asOptionalText(source.agent_id);
  const sessionId = asOptionalText(source.session_id);
  if (agentId === null || sessionId === null) {
    return null;
  }

  return {
    ...source,
    agent_id: agentId,
    session_id: sessionId,
    project_id: asOptionalText(source.project_id),
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
