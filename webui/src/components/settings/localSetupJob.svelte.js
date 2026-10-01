// The installation job behind one local target (`local/...`) of any task:
// the state the server reports, polled while it installs or while the server
// restarts, and the install and restart actions. Local speech support and the
// Conversation search panel's local Models present it; each creates one
// during component initialization, because it registers its lifecycle with
// the creating component.

import { onDestroy, onMount } from 'svelte';

import { getLocalSetupStatus, installLocalSetup } from '$lib/api.js';

const POLL_INTERVAL_MS = 1500;
const RESTART_TIMEOUT_MS = 90_000;

/**
 * @param {object} params
 * @param {() => string} params.getTarget - The local target id.
 * @param {() => unknown} [params.onReady] - Called, and awaited, whenever a
 *   status reports `ready`.
 * @param {((target: string) => Promise<{state: string, error?: string}>) | null} [params.restart]
 *   Asks the server to restart after an installation that needs it; null
 *   when the target's task has no restart step.
 */
export function createLocalSetupJob({
  getTarget,
  onReady = () => {},
  restart = null,
}) {
  // The last status: `{ state, phase, error, progress?, restart_available }`;
  // `state` reads `restarting` while a requested restart is awaited.
  let status = $state(null);
  // A client-side problem: `connection`, `restart_timeout` or the restart
  // request's error code; '' when none.
  let error = $state('');
  let acting = $state(false);
  let restartStartedAt = 0;
  let timer = null;
  let requestId = 0;
  let destroyed = false;

  onMount(() => {
    void refresh();
  });

  onDestroy(() => {
    destroyed = true;
    clearTimeout(timer);
    requestId += 1;
  });

  function scheduleRefresh() {
    clearTimeout(timer);
    if (!destroyed) {
      timer = setTimeout(() => void refresh(), POLL_INTERVAL_MS);
    }
  }

  async function refresh() {
    const current = ++requestId;
    try {
      const next = await getLocalSetupStatus(getTarget());
      if (destroyed || current !== requestId) return;
      error = '';
      status =
        restartStartedAt && next.state !== 'ready'
          ? { ...next, state: 'restarting' }
          : next;
      if (next.state === 'ready') {
        restartStartedAt = 0;
        await onReady();
      }
    } catch {
      if (destroyed || current !== requestId) return;
      // The server is expected to be unreachable while it restarts.
      if (!restartStartedAt) error = 'connection';
    }
    if (
      restartStartedAt &&
      Date.now() - restartStartedAt > RESTART_TIMEOUT_MS
    ) {
      error = 'restart_timeout';
      return;
    }
    if (restartStartedAt || status?.state === 'installing') scheduleRefresh();
  }

  async function install() {
    if (acting || status?.state === 'installing') return;
    acting = true;
    error = '';
    clearTimeout(timer);
    requestId += 1;
    try {
      const next = await installLocalSetup(getTarget());
      if (destroyed) return;
      status = next;
      scheduleRefresh();
    } catch {
      if (!destroyed) {
        error = 'connection';
        scheduleRefresh();
      }
    } finally {
      if (!destroyed) acting = false;
    }
  }

  async function restartServer() {
    if (acting || restart === null) return;
    acting = true;
    error = '';
    requestId += 1;
    restartStartedAt = Date.now();
    try {
      const result = await restart(getTarget());
      if (destroyed) return;
      if (result.state !== 'restarting') {
        restartStartedAt = 0;
        error = result.error || 'restart_unavailable';
      } else {
        status = { ...status, state: 'restarting' };
      }
    } catch {
      // The response may have been interrupted by the requested restart.
      // Inspect status, never automatically repeat the restart mutation.
      if (!destroyed) status = { ...status, state: 'restarting' };
    } finally {
      if (!destroyed) {
        acting = false;
        scheduleRefresh();
      }
    }
  }

  return {
    // `checking` until the first status arrives.
    get state() {
      return status?.state ?? 'checking';
    },
    get status() {
      return status;
    },
    get error() {
      return error;
    },
    get acting() {
      return acting;
    },
    refresh,
    install,
    restartServer,
  };
}
