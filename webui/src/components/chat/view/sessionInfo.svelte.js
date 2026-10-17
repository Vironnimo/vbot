import { untrack } from 'svelte';

import {
  runChangeStats,
  sessionChangeStats,
} from '$lib/chatTimelinePresentation.js';
import { sessionDisplayName } from '$lib/sessionListView.js';

// What the Session information panel shows about the displayed Session itself:
// its list row (name, creation, last activity; `session.get`) and its change
// statistics (`session.change_stats`, summed over all of the Session's own
// Runs, not only the loaded History). The server owns both, so they are read
// while the panel shows them: when it opens, when the displayed Session or a
// Sessions refresh changes, and whenever a loaded Run's own statistics change.
// The server stores a Run's statistics before it streams them, so a read
// prompted by a live update already includes it.
//
// `stats` is undefined until the first read for the displayed Session answers,
// then null (no changes) or the presented statistics. `summary` is null until
// its read answers.
export function createSessionInfo(context) {
  let wanted = $state(false);
  let stats = $state.raw(undefined);
  let summary = $state.raw(null);
  let infoSessionKey = '';
  let requestedKey = '';

  let runRevision = $derived(runStatsRevision(context.timelineItems));

  $effect(() => {
    const open = wanted;
    const addressing = context.target.activeAddressing();
    const displayKey = context.target.displayedSessionKey();
    // Closed, the panel does not follow the streaming timeline at all.
    const key = open
      ? `${displayKey}::${context.sessionsRefreshToken}::${runRevision}`
      : '';
    untrack(() => {
      if (displayKey !== infoSessionKey) {
        infoSessionKey = displayKey;
        stats = undefined;
        summary = null;
        requestedKey = '';
      }
      if (!open) {
        // Reopening reads again; what the panel showed stays until then.
        requestedKey = '';
        return;
      }
      if (
        !displayKey ||
        !addressing.agentAddress ||
        !addressing.sessionId ||
        key === requestedKey
      ) {
        return;
      }
      requestedKey = key;
      loadStats(key, addressing.agentAddress, addressing.sessionId);
      loadSummary(key, addressing.agentAddress, addressing.sessionId);
    });
  });

  async function loadStats(key, agentAddress, sessionId) {
    try {
      const result = await context.chatController.getSessionChangeStats(
        agentAddress,
        sessionId,
      );
      // A newer Session, refresh or Run update superseded this read.
      if (requestedKey === key) {
        stats = sessionChangeStats(result?.change_stats);
      }
    } catch {
      // Best effort: the panel keeps the statistics it showed last.
      if (requestedKey === key && stats === undefined) {
        stats = null;
      }
    }
  }

  async function loadSummary(key, agentAddress, sessionId) {
    try {
      const result = await context.chatController.getSession(
        agentAddress,
        sessionId,
      );
      const session = result?.session;
      if (requestedKey === key && session) {
        summary = {
          displayName: sessionDisplayName(session),
          createdAt: session.created_at ?? null,
          lastActiveAt: session.last_active_at ?? null,
        };
      }
    } catch {
      // Best effort: the panel keeps the row it showed last.
    }
  }

  return {
    get stats() {
      return stats;
    },
    get summary() {
      return summary;
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
