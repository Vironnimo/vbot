import { untrack } from 'svelte';

import { isRunActive } from '$lib/chatState.js';
import { applySessionCompletionActivity } from '$lib/chatState/sessionState.js';
import {
  runChangeStats,
  sessionChangeStats,
} from '$lib/chatTimelinePresentation.js';
import { sessionDisplayName } from '$lib/sessionListView.js';

// Live progress reads again at most this often; opening the panel, another
// Session, a Sessions refresh and the end of the displayed Session's Run read
// at once. Every Model request reports progress, and the tree-wide usage read
// is the server's most expensive one, so a shorter interval costs the server
// far more than the panel gains.
const LIVE_READ_INTERVAL_MS = 10_000;

// What the Session information panel shows about the displayed Session and
// the Sub-Agents below it: its list row (name, creation, last activity;
// `session.get`), its Sub-Agent tree (`subagent.tree`), and its change
// statistics and usage summed over its own Runs and those of its Sub-Agents
// at every depth (`session.change_stats` with `include_subagents`,
// `statistics.session_usage`), not only the loaded History. The server owns
// all of it, so it is read while the panel shows it: when it opens, when the
// displayed Session or a Sessions refresh changes, when the displayed
// Session's Run ends, and as work progresses - a loaded Run's own statistics
// or the Session's usage change, or a Sub-Agent status arrives. Reads run one
// at a time; progress arriving meanwhile is read once the last read ended and
// the interval passed, so the last progress is always read. The server stores
// a Run's statistics and usage before it streams them, so a read prompted by
// a live update or the Run's end already includes them.
//
// The Run events carry only the Session's own token counts, without cost and
// without its Sub-Agents, so the panel's usage cannot follow them and stays a
// server read.
//
// `stats` is undefined until the first read for the displayed Session answers,
// then null (no changes) or the presented statistics. `summary` and `usage`
// are null until their reads answer; `subAgents` is empty until then.
export function createSessionInfo(context) {
  let wanted = $state(false);
  let stats = $state.raw(undefined);
  let summary = $state.raw(null);
  let usage = $state.raw(null);
  let subAgents = $state.raw([]);
  let infoSessionKey = '';
  let baseKey = '';
  let liveKey = '';
  let pending = null;
  let reading = false;
  let lastReadAt = 0;
  let timer = null;
  // Whether the displayed Session's Run was active at the last look.
  let wasRunning = false;

  let runRevision = $derived(runStatsRevision(context.timelineItems));
  let usageRevision = $derived(sessionUsageRevision(context.sessionUsage));

  $effect(() => {
    const open = wanted;
    const addressing = context.target.activeAddressing();
    const displayKey = context.target.displayedSessionKey();
    // Closed, the panel does not follow the streaming timeline at all.
    const base = open ? `${displayKey}::${context.sessionsRefreshToken}` : '';
    const live = open
      ? `${runRevision}::${usageRevision}::${statusesRevision(context.subAgentStatuses)}`
      : '';
    const running = open && isRunActive(context.target.activeSessionState);
    untrack(() => {
      if (displayKey !== infoSessionKey) {
        infoSessionKey = displayKey;
        stats = undefined;
        summary = null;
        usage = null;
        subAgents = [];
        baseKey = '';
        wasRunning = false;
      }
      // The Run's end reads its final figures without waiting.
      const runEnded = wasRunning && !running;
      wasRunning = running;
      if (!open) {
        // Reopening reads again; what the panel showed stays until then.
        baseKey = '';
        pending = null;
        return;
      }
      if (!displayKey || !addressing.agentAddress || !addressing.sessionId) {
        return;
      }
      const urgent = base !== baseKey || runEnded;
      if (!urgent && live === liveKey) {
        return;
      }
      baseKey = base;
      liveKey = live;
      pending = {
        sessionKey: displayKey,
        agentAddress: addressing.agentAddress,
        sessionId: addressing.sessionId,
        urgent: urgent || Boolean(pending?.urgent),
      };
      pump();
    });
  });

  $effect(() => () => {
    clearTimeout(timer);
    timer = null;
    pending = null;
  });

  function pump() {
    if (reading || !pending) {
      return;
    }
    if (timer) {
      if (!pending.urgent) {
        return;
      }
      clearTimeout(timer);
      timer = null;
    }
    const wait = pending.urgent
      ? 0
      : lastReadAt + LIVE_READ_INTERVAL_MS - Date.now();
    if (wait > 0) {
      timer = setTimeout(() => {
        timer = null;
        pump();
      }, wait);
      return;
    }
    const job = pending;
    pending = null;
    reading = true;
    lastReadAt = Date.now();
    read(job).finally(() => {
      reading = false;
      pump();
    });
  }

  // Reads answer one at a time, so a later answer for the same Session is
  // always the newer one; one for another Session is dropped.
  async function read(job) {
    const { agentAddress, sessionId } = job;
    const current = () => job.sessionKey === infoSessionKey;
    const controller = context.chatController;
    await Promise.all([
      controller
        .getSessionChangeStats(agentAddress, sessionId, {
          includeSubAgents: true,
        })
        .then((result) => {
          if (current()) stats = sessionChangeStats(result?.change_stats);
        })
        .catch(() => {
          // Best effort: the panel keeps the statistics it showed last.
          if (current() && stats === undefined) stats = null;
        }),
      controller
        .getSessionUsage(agentAddress, sessionId)
        .then((result) => {
          if (current() && result?.total) {
            usage = {
              session: result.session ?? null,
              subAgents: result.subagents ?? null,
              total: result.total,
              subAgentCount: Number(result.subagent_count) || 0,
            };
          }
        })
        .catch(() => {}),
      controller
        .getSubAgentTree(agentAddress, sessionId)
        .then((result) => {
          if (current() && Array.isArray(result?.subagents)) {
            subAgents = result.subagents;
          }
        })
        .catch(() => {}),
      controller
        .getSession(agentAddress, sessionId)
        .then((result) => {
          const session = result?.session;
          if (current() && session) {
            applySessionCompletionActivity(
              context.target.activeSessionState,
              session,
            );
            summary = {
              displayName: sessionDisplayName(session),
              createdAt: session.created_at ?? null,
              lastActiveAt: session.last_active_at ?? null,
            };
          }
        })
        .catch(() => {}),
    ]);
  }

  return {
    get stats() {
      return stats;
    },
    get summary() {
      return summary;
    },
    get usage() {
      return usage;
    },
    get subAgents() {
      return subAgents;
    },
    setWanted(value) {
      wanted = Boolean(value);
    },
  };
}

// Changes whenever a loaded Run's own statistics change: live while it runs,
// final at its end, and when History adds or replaces Runs.
function runStatsRevision(items) {
  return (items ?? [])
    .filter((item) => item?.type === 'assistant_run')
    .map((item) => {
      const stats = runChangeStats(item);
      return stats ? `${stats.files}:${stats.added}:${stats.removed}` : '-';
    })
    .join('|');
}

// Changes with every model request the Session's own turns report.
function sessionUsageRevision(sessionUsage) {
  return `${sessionUsage?.input_tokens ?? 0}:${sessionUsage?.output_tokens ?? 0}`;
}

// The Sub-Agent status cache is replaced whenever a Sub-Agent's state changes;
// each new cache counts as progress.
const statusRevisions = new WeakMap();
let nextStatusRevision = 0;
function statusesRevision(statuses) {
  if (!statuses || typeof statuses !== 'object') {
    return 0;
  }
  let revision = statusRevisions.get(statuses);
  if (revision === undefined) {
    revision = ++nextStatusRevision;
    statusRevisions.set(statuses, revision);
  }
  return revision;
}
