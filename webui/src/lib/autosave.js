import { getContext, setContext } from 'svelte';

const AUTOSAVE_CONTEXT = Symbol('vbot-autosave');
const MAX_STABLE_SAVE_PASSES = 10;

export const DEFAULT_AUTOSAVE_DEBOUNCE_MS = 800;

const composingInputs = new WeakSet();

// Shared controls report composition without making draft values global.
export function autosaveInput(node) {
  const start = () => composingInputs.add(node);
  const end = () => composingInputs.delete(node);
  node.addEventListener('compositionstart', start);
  node.addEventListener('compositionend', end);
  return {
    destroy() {
      end();
      node.removeEventListener('compositionstart', start);
      node.removeEventListener('compositionend', end);
    },
  };
}

/** The common input-aware timer, also used by collection editors. */
export function scheduleAutosave(run, delay = DEFAULT_AUTOSAVE_DEBOUNCE_MS) {
  const input = typeof document === 'undefined' ? null : document.activeElement;
  let timer = null;
  let cancelled = false;
  const cancel = () => {
    cancelled = true;
    clearTimeout(timer);
    input?.removeEventListener('blur', finish);
    input?.removeEventListener('compositionend', resume);
  };
  const finish = () => {
    if (cancelled) return;
    cancel();
    void run();
  };
  const attempt = () => {
    const focused = input && input.ownerDocument.activeElement === input;
    if (
      focused &&
      (input.type === 'number' ||
        input.inputMode === 'decimal' ||
        composingInputs.has(input))
    ) {
      input.addEventListener('blur', finish, { once: true });
      input.addEventListener('compositionend', resume, { once: true });
      return;
    }
    finish();
  };
  const resume = () => {
    clearTimeout(timer);
    timer = setTimeout(attempt, delay);
  };
  timer = setTimeout(attempt, delay);
  return cancel;
}

function snapshotKey(value) {
  return JSON.stringify(value);
}

export function createAutosaveParticipant({
  cancelPending = () => {},
  getSnapshot,
  hasChanges,
  save,
}) {
  let activeSave = null;
  let failedSnapshot = null;
  let lastSuccessfulSnapshot = null;

  function runSave(reason = 'auto', { force = false } = {}) {
    if (reason !== 'auto') cancelPending();
    if (activeSave) {
      return activeSave.then((succeeded) => {
        if (
          !succeeded &&
          reason === 'auto' &&
          snapshotKey(getSnapshot()) === failedSnapshot
        ) {
          return false;
        }
        const currentSnapshot = snapshotKey(getSnapshot());
        if (hasChanges() && currentSnapshot !== lastSuccessfulSnapshot) {
          return runSave(reason, { force });
        }
        return true;
      });
    }
    if (!force && !hasChanges()) {
      return Promise.resolve(true);
    }

    const savedSnapshot = snapshotKey(getSnapshot());
    if (
      !force &&
      reason === 'auto' &&
      savedSnapshot === lastSuccessfulSnapshot
    ) {
      return Promise.resolve(true);
    }
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

/**
 * One autosave participant plus its debounce timer, owned together.
 *
 * The timer is deliberately not reactive state - it only holds a setTimeout
 * handle. `cancelPending` is wired into the participant automatically so a
 * save-flush (or a manual run) always cancels the pending debounce first;
 * components keep only their own reactive trigger:
 *
 *   const editor = createDebouncedAutosave({ getSnapshot, hasChanges, save });
 *   $effect(() => {
 *     if (saveDisabled) return;
 *     editor.scheduleRun();
 *     return editor.cancelPendingTimer;
 *   });
 */
export function createDebouncedAutosave({
  getSnapshot,
  hasChanges,
  save,
  debounceMs = DEFAULT_AUTOSAVE_DEBOUNCE_MS,
}) {
  let timer = null;

  function cancelPendingTimer() {
    if (timer !== null) {
      timer();
      timer = null;
    }
  }

  function scheduleRun() {
    // Read the full draft in the caller's reactive effect: a boolean dirty
    // flag alone stops changing after the first keystroke.
    snapshotKey(getSnapshot());
    cancelPendingTimer();
    timer = scheduleAutosave(() => {
      timer = null;
      void participant.runSave();
    }, debounceMs);
  }

  const participant = createAutosaveParticipant({
    cancelPending: cancelPendingTimer,
    getSnapshot,
    hasChanges,
    save,
  });

  return { cancelPendingTimer, participant, scheduleRun };
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
  const context = getContext(AUTOSAVE_CONTEXT);
  if (context) return context;
  const coordinator = createAutosaveCoordinator();
  return {
    register: coordinator.register,
    requestTransition: (action) => {
      if (!coordinator.hasPending()) return action();
      return coordinator
        .flushPending()
        .then((saved) => (saved ? action() : false));
    },
  };
}
