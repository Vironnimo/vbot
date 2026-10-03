import { untrack } from 'svelte';

import {
  runChangeStats,
  sessionChangeStats,
} from '$lib/chatTimelinePresentation.js';

// The displayed Session's change statistics for the Activity panel. The server
// owns them (`session.change_stats`, summed over all of the Session's own
// Runs, not only the loaded History), so they are read while the panel shows
// them: when it opens, when the displayed Session or a Sessions refresh
// changes, and whenever a loaded Run's own statistics change. The server
// stores a Run's statistics before it streams them, so a read prompted by a
// live update already includes it.
//
// `stats` is undefined until the first read for the displayed Session answers,
// then null (no changes) or the presented statistics.
export function createSessionChanges(context) {
  let wanted = $state(false);
  let stats = $state.raw(undefined);
  let statsSessionKey = '';
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
      if (displayKey !== statsSessionKey) {
        statsSessionKey = displayKey;
        stats = undefined;
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
      load(key, addressing.agentAddress, addressing.sessionId);
    });
  });

  async function load(key, agentAddress, sessionId) {
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

  return {
    get stats() {
      return stats;
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
