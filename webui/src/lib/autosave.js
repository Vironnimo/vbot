import { getContext, setContext } from 'svelte';

const AUTOSAVE_CONTEXT = Symbol('vbot-autosave');
const MAX_STABLE_SAVE_PASSES = 10;

const fallbackContext = Object.freeze({
  register: () => () => {},
  requestTransition: (action) => action(),
});

function snapshotKey(value) {
  return JSON.stringify(value);
}

export function createAutosaveParticipant({
  cancelPending,
  getSnapshot,
  hasChanges,
  save,
}) {
  let activeSave = null;
  let failedSnapshot = null;
  let lastSuccessfulSnapshot = null;

  function runSave(reason = 'auto', { force = false } = {}) {
    if (activeSave) {
      return activeSave.then((succeeded) => {
        if (!succeeded) {
          return false;
        }
        const currentSnapshot = snapshotKey(getSnapshot());
        if (hasChanges() && currentSnapshot !== lastSuccessfulSnapshot) {
          return runSave(reason);
        }
        return true;
      });
    }
    if (!force && !hasChanges()) {
      return Promise.resolve(true);
    }

    const savedSnapshot = snapshotKey(getSnapshot());
    if (reason === 'auto' && savedSnapshot === failedSnapshot) {
      return Promise.resolve(false);
    }
    let resolveOperation;
    const operation = new Promise((resolve) => {
      resolveOperation = resolve;
    });
    activeSave = operation;

    const complete = (succeeded) => {
      if (succeeded) {
        failedSnapshot = null;
        lastSuccessfulSnapshot = savedSnapshot;
      } else {
        failedSnapshot = savedSnapshot;
      }
      if (activeSave === operation) {
        activeSave = null;
      }
      resolveOperation(succeeded);
    };

    try {
      Promise.resolve(save(reason)).then(
        (result) => complete(result === true),
        () => complete(false),
      );
    } catch {
      complete(false);
    }

    return operation;
  }

  async function flush() {
    cancelPending();

    for (let pass = 0; pass < MAX_STABLE_SAVE_PASSES; pass += 1) {
      if (activeSave && !(await activeSave)) {
        return false;
      }

      const currentSnapshot = snapshotKey(getSnapshot());
      if (!hasChanges() || currentSnapshot === lastSuccessfulSnapshot) {
        return true;
      }
      if (!(await runSave('transition'))) {
        return false;
      }
    }

    return false;
  }

  return {
    flush,
    hasPending: () =>
      activeSave !== null ||
      (hasChanges() && snapshotKey(getSnapshot()) !== lastSuccessfulSnapshot),
    runSave,
  };
}

export function createAutosaveCoordinator() {
  const participants = new Set();

  function register(participant) {
    participants.add(participant);
    return () => {
      participants.delete(participant);
    };
  }

  function hasPending() {
    return Array.from(participants).some(
      (participant) => participant.hasPending?.() === true,
    );
  }

  async function flushPending() {
    for (let pass = 0; pass < MAX_STABLE_SAVE_PASSES; pass += 1) {
      const pending = Array.from(participants).filter(
        (participant) => participant.hasPending?.() === true,
      );
      if (pending.length === 0) {
        return true;
      }

      const results = await Promise.all(
        pending.map((participant) =>
          Promise.resolve()
            .then(() => participant.flush())
            .then((result) => result === true)
            .catch(() => false),
        ),
      );
      if (!results.every(Boolean)) {
        return false;
      }
    }

    return false;
  }

  return {
    flushPending,
    hasPending,
    register,
  };
}

export function provideAutosaveContext(context) {
  setContext(AUTOSAVE_CONTEXT, context);
}

export function useAutosaveContext() {
  return getContext(AUTOSAVE_CONTEXT) ?? fallbackContext;
}
