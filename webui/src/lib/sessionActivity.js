import { qualifyAgentAddress } from './agentAddress.js';
import { activeLocaleTag, t } from './i18n.js';
import { asOptionalText, isPlainObject } from './values.js';

// The server-owned Session activity (`server.md`): a Session is `working`
// (an active Run or queued input), `waiting` (no Run, but pending work that
// will wake it: Sub-Agents answering it, background commands whose result it
// will receive, delivered results not yet taken up) or `idle`. Accessors read
// this one state and never rebuild it from Run or command events.
//
// App keeps the non-idle Sessions as one plain object keyed like Chat's
// Session state (`<agent address>::<session id>`), so `activity[sessionKey]`
// finds a held Session's entry. `connection_ready.session_activity` replaces
// it, each `session_activity_changed` updates one entry, and `idle` removes
// it. An entry is `{ agentAddress, sessionId, state, workStartedAt,
// waitingFor: { subagents, commands, results }, contributesToAgentActivity }`.

export const SESSION_WORKING = 'working';
export const SESSION_WAITING = 'waiting';
export const SESSION_IDLE = 'idle';

const ACTIVE_STATES = new Set([SESSION_WORKING, SESSION_WAITING]);
const WAITING_KINDS = ['subagents', 'commands', 'results'];

export function sessionActivityKey(agentAddress, sessionId) {
  return `${agentAddress}::${sessionId}`;
}

export function normalizeActivityState(value) {
  return ACTIVE_STATES.has(value) ? value : SESSION_IDLE;
}

// What a waiting Session waits for; every count a whole number, 0 if absent.
export function normalizeWaitingFor(value) {
  const source = isPlainObject(value) ? value : {};
  return Object.fromEntries(
    WAITING_KINDS.map((kind) => [
      kind,
      Number.isSafeInteger(source[kind]) && source[kind] > 0 ? source[kind] : 0,
    ]),
  );
}

// One server row (a hello entry or a change event's payload) as its key and
// entry; null when it names no Session. Idle rows yield an idle entry.
function parseActivityRow(row) {
  if (!isPlainObject(row)) {
    return null;
  }
  const agentId = asOptionalText(row.agent_id);
  const sessionId = asOptionalText(row.session_id);
  if (agentId === null || sessionId === null) {
    return null;
  }
  const agentAddress = qualifyAgentAddress(agentId, row.project_id);
  const state = normalizeActivityState(row.state);
  return {
    key: sessionActivityKey(agentAddress, sessionId),
    entry: {
      agentAddress,
      sessionId,
      state,
      workStartedAt:
        state === SESSION_IDLE ? null : asOptionalText(row.work_started_at),
      waitingFor: normalizeWaitingFor(
        state === SESSION_WAITING ? row.waiting_for : null,
      ),
      contributesToAgentActivity: row.contributes_to_agent_activity !== false,
    },
  };
}

// The store from `connection_ready.session_activity`, which lists every
// non-idle Session.
export function sessionActivityFromSnapshot(rows) {
  const activity = {};
  for (const row of Array.isArray(rows) ? rows : []) {
    const parsed = parseActivityRow(row);
    if (parsed && parsed.entry.state !== SESSION_IDLE) {
      activity[parsed.key] = parsed.entry;
    }
  }
  return activity;
}

// The store after one `session_activity_changed` payload: a fresh object, so
// readers of the previous one see no change.
export function applySessionActivityChange(activity, payload) {
  const parsed = parseActivityRow(payload);
  const current = isPlainObject(activity) ? activity : {};
  if (parsed === null) {
    return current;
  }
  const next = { ...current };
  if (parsed.entry.state === SESSION_IDLE) {
    delete next[parsed.key];
  } else {
    next[parsed.key] = parsed.entry;
  }
  return next;
}

// An Identity Agent rename keeps its Sessions: their entries move to the new
// address. A Project Agent's `agent@project` address is not an Identity id.
export function renameSessionActivityAgent(activity, oldAgentId, newAgentId) {
  if (!isPlainObject(activity) || !oldAgentId || !newAgentId) {
    return activity;
  }
  const entries = Object.values(activity);
  if (!entries.some((entry) => entry.agentAddress === oldAgentId)) {
    return activity;
  }
  return Object.fromEntries(
    entries.map((entry) => {
      const moved =
        entry.agentAddress === oldAgentId
          ? { ...entry, agentAddress: newAgentId }
          : entry;
      return [sessionActivityKey(moved.agentAddress, moved.sessionId), moved];
    }),
  );
}

// What an Agent's Sessions are doing together, or null when they are idle:
// `working` when one works and counts toward Agent activity, else `waiting`
// when one waits (waiting always counts); `workStartedAt` is the earliest
// start among them and `waitingFor` sums the waiting Sessions.
export function agentSessionActivity(activity, agentAddress) {
  let working = false;
  let waiting = false;
  let workStartedAt = null;
  let earliestStart = Infinity;
  const waitingFor = normalizeWaitingFor(null);
  for (const entry of Object.values(activity ?? {})) {
    if (entry?.agentAddress !== agentAddress) {
      continue;
    }
    if (entry.state === SESSION_WORKING) {
      if (entry.contributesToAgentActivity === false) {
        continue;
      }
      working = true;
    } else if (entry.state === SESSION_WAITING) {
      waiting = true;
      for (const kind of WAITING_KINDS) {
        waitingFor[kind] += entry.waitingFor?.[kind] ?? 0;
      }
    } else {
      continue;
    }
    const startedAt = timestampMs(entry.workStartedAt);
    if (startedAt !== null && startedAt < earliestStart) {
      earliestStart = startedAt;
      workStartedAt = entry.workStartedAt;
    }
  }
  if (!working && !waiting) {
    return null;
  }
  return {
    state: working ? SESSION_WORKING : SESSION_WAITING,
    workStartedAt,
    waitingFor,
  };
}

// The span since the work started, in milliseconds; null when unknown.
export function workSpanMs(workStartedAt, nowMs) {
  const startedAt = timestampMs(workStartedAt);
  if (startedAt === null || !Number.isFinite(nowMs)) {
    return null;
  }
  return Math.max(0, nowMs - startedAt);
}

// A live span in whole units: "12s", "4m 12s", "1h 4m".
export function formatWorkSpan(spanMs) {
  if (!Number.isFinite(spanMs) || spanMs < 0) {
    return '';
  }
  const totalSeconds = Math.floor(spanMs / 1000);
  if (totalSeconds < 60) {
    return t('chat.durationSeconds', { seconds: totalSeconds });
  }
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  if (hours > 0) {
    return t('chat.durationHoursMinutes', { hours, minutes });
  }
  return t('chat.durationMinutesSeconds', {
    minutes,
    seconds: totalSeconds % 60,
  });
}

// "Waiting for 2 Sub-Agents, 1 command"; "Waiting" when no count is known.
export function waitingForText(waitingFor) {
  const counts = normalizeWaitingFor(waitingFor);
  const items = [];
  if (counts.subagents > 0) {
    items.push(
      counts.subagents === 1
        ? t('chat.sessionActivity.subagentsOne')
        : t('chat.sessionActivity.subagentsCount', {
            count: counts.subagents,
          }),
    );
  }
  if (counts.commands > 0) {
    items.push(
      counts.commands === 1
        ? t('chat.sessionActivity.commandsOne')
        : t('chat.sessionActivity.commandsCount', { count: counts.commands }),
    );
  }
  if (counts.results > 0) {
    items.push(
      counts.results === 1
        ? t('chat.sessionActivity.resultsOne')
        : t('chat.sessionActivity.resultsCount', { count: counts.results }),
    );
  }
  return items.length > 0
    ? t('chat.sessionActivity.waitingFor', { items: formatList(items) })
    : t('chat.sessionActivity.waiting');
}

// A working or waiting Session (or Agent) in words, without its span.
export function sessionActivityLabel(activity) {
  if (activity?.state === SESSION_WORKING) {
    return t('chat.sessionActivity.working');
  }
  if (activity?.state === SESSION_WAITING) {
    return waitingForText(activity.waitingFor);
  }
  return '';
}

// "Working · 4m 12s", "Waiting for 2 Sub-Agents · 4m 12s"; the words alone
// while the start is unknown, '' while idle.
export function sessionActivityText(activity, nowMs = Date.now()) {
  const label = sessionActivityLabel(activity);
  const span = formatWorkSpan(workSpanMs(activity?.workStartedAt, nowMs));
  return label && span
    ? t('chat.sessionActivity.withSpan', { state: label, span })
    : label;
}

function formatList(items) {
  try {
    return new Intl.ListFormat(activeLocaleTag(), {
      style: 'short',
      type: 'unit',
    }).format(items);
  } catch {
    return items.join(', ');
  }
}

function timestampMs(value) {
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isNaN(parsed) ? null : parsed;
}
