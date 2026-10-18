import {
  AUTOSAVE_STILL_SAVING_MS,
  createAutosaveCoordinator,
} from '$lib/autosave.js';

// App's pending-navigation lifecycle. The coordinator owns editor writes;
// this owner decides whether the latest requested transition may proceed.
export function createAppAutosave({ onStay }) {
  const autosaveCoordinator = createAutosaveCoordinator();
  // A navigation waits here while pending edits save. `autosavePrompt` is
  // null, 'slow' (a save takes long: Keep waiting / Leave anyway) or
  // 'failed' (Retry / Discard and continue).
  let autosaveTransitionSaving = $state(false);
  let autosavePrompt = $state(null);
  let pendingAutosaveTransition = null;
  // Each flush belongs to one transition; one the user left behind cannot
  // navigate or reopen a prompt when it settles.
  let autosaveTransitionId = 0;
  let autosaveTransitionSlow = false;
  let autosaveSlowTimer = null;
  const stopAutosaveSlowTimer = () => {
    clearTimeout(autosaveSlowTimer);
    autosaveSlowTimer = null;
    autosaveTransitionSlow = false;
  };

  // Ends the waiting transition without running it; a flush still running
  // for it settles unobserved.
  const closeAutosaveTransition = () => {
    autosaveTransitionId += 1;
    stopAutosaveSlowTimer();
    autosaveTransitionSaving = false;
    autosavePrompt = null;
    pendingAutosaveTransition = null;
  };

  const runAutosaveTransition = async (action) => {
    const transitionId = ++autosaveTransitionId;
    pendingAutosaveTransition = action;
    autosaveTransitionSaving = true;
    stopAutosaveSlowTimer();
    // A save that takes long offers to leave; an open failure prompt (a
    // Retry) already offers it.
    autosaveSlowTimer = setTimeout(() => {
      autosaveSlowTimer = null;
      if (transitionId !== autosaveTransitionId) return;
      autosaveTransitionSlow = true;
      autosavePrompt ??= 'slow';
    }, AUTOSAVE_STILL_SAVING_MS);
    const saved = await autosaveCoordinator.flushPending();
    if (transitionId !== autosaveTransitionId) return false;
    stopAutosaveSlowTimer();
    autosaveTransitionSaving = false;

    if (!saved) {
      autosavePrompt = 'failed';
      return false;
    }

    const latestAction = pendingAutosaveTransition;
    pendingAutosaveTransition = null;
    autosavePrompt = null;
    return latestAction?.();
  };

  const requestAutosaveTransition = (action) => {
    if (typeof action !== 'function' || autosavePrompt === 'failed') {
      return false;
    }
    if (autosaveTransitionSaving) {
      pendingAutosaveTransition = action;
      // The save already took long: ask again instead of waiting silently.
      if (autosaveTransitionSlow) autosavePrompt ??= 'slow';
      return false;
    }
    if (!autosaveCoordinator.hasPending()) {
      return action();
    }
    return runAutosaveTransition(action);
  };

  const retryAutosaveTransition = () => {
    if (!pendingAutosaveTransition || autosaveTransitionSaving) {
      return;
    }
    void runAutosaveTransition(pendingAutosaveTransition);
  };

  // Keep waiting: the navigation still runs once the save finishes.
  const keepWaitingForAutosave = () => {
    if (autosavePrompt === 'slow') autosavePrompt = null;
  };

  // Leave anyway / Discard and continue: the latest navigation runs now,
  // also while a save still runs. Running saves finish in the background.
  const leaveAutosaveTransition = () => {
    const action = pendingAutosaveTransition;
    if (!action) return;
    closeAutosaveTransition();
    autosaveCoordinator.releaseRunningSaves();
    action();
  };

  // Closing the failure prompt stays with the unsaved edits: the navigation
  // is dropped, and a pending Back/Forward returns to the shown place.
  const stayAfterAutosaveFailure = () => {
    if (autosavePrompt !== 'failed') return;
    closeAutosaveTransition();
    onStay();
  };

  // Whether pending edits saved within the time a navigation waits before it
  // offers to leave. A slower save keeps running.
  const flushPendingWithinStillSavingLimit = () => {
    let timer;
    const limit = new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), AUTOSAVE_STILL_SAVING_MS);
    });
    return Promise.race([autosaveCoordinator.flushPending(), limit]).finally(
      () => clearTimeout(timer),
    );
  };

  return {
    get prompt() {
      return autosavePrompt;
    },
    get saving() {
      return autosaveTransitionSaving;
    },
    register: autosaveCoordinator.register,
    hasPending: autosaveCoordinator.hasPending,
    requestTransition: requestAutosaveTransition,
    retry: retryAutosaveTransition,
    keepWaiting: keepWaitingForAutosave,
    leave: leaveAutosaveTransition,
    stay: stayAfterAutosaveFailure,
    flushWithinLimit: flushPendingWithinStillSavingLimit,
    destroy: closeAutosaveTransition,
  };
}
