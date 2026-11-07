import { onDestroy, untrack } from 'svelte';
import { createDebouncedAutosave } from '../../../../webui/src/lib/autosave.js';
import { rebaseDraft } from '../../../../webui/src/lib/settingsSave.js';
import { t } from '../../../../webui/src/lib/i18n.js';
import { copy, failureText } from './presentation.js';

// Factory's versioned record editor. The shared helper owns timing and save
// serialization; this owner projects fields and rebases server revisions.
export function createRecordEditor(options) {
  let baseline = $state(
    untrack(() => options.project(options.record ?? options.defaults)),
  );
  let draft = $state(untrack(() => copy(baseline)));
  let revision = $state(untrack(() => options.record?.revision ?? 0));
  let exists = $state(untrack(() => (options.record?.revision ?? 0) > 0));
  let saving = $state(false);
  let error = $state('');
  let notice = $state('');
  let deferred = null;
  let disposed = false;
  const fingerprint = (value) => JSON.stringify(options.normalize(value));
  const hasChanges = () => fingerprint(draft) !== fingerprint(baseline);

  function adopt(record) {
    if (!record) return;
    const incomingRevision = record.revision ?? 0;
    if (
      (incomingRevision === 0 && !exists) ||
      (incomingRevision > 0 && incomingRevision <= revision)
    )
      return;
    if (saving) {
      deferred = record;
      return;
    }
    if ((record.revision ?? 0) === 0 && exists) {
      baseline = options.project(record);
      revision = 0;
      exists = false;
      notice = t('factory.setupDeleted');
      return;
    }
    const saved = options.project(record);
    const merged = rebaseDraft(copy(draft), copy(baseline), saved);
    baseline = saved;
    revision = record.revision ?? 0;
    exists = true;
    draft = merged.value;
    if (merged.conflicts.length) notice = t('factory.saveConflict');
  }

  async function persist() {
    error = options.validate(draft) || '';
    if (error) return false;
    saving = true;
    try {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const submitted = copy(draft);
        let record;
        try {
          record = await options.save(
            options.normalize(submitted),
            revision,
            options.normalize(baseline),
          );
        } catch (cause) {
          if (
            attempt >= 2 ||
            ![
              'revision_conflict',
              'factory_conflict',
              'stale_revision',
            ].includes(cause?.code)
          )
            throw cause;
          const current = await options.read();
          if ((current.revision ?? 0) === 0 && exists) {
            deferred = current;
            return false;
          }
          const saved = options.project(current);
          const merged = rebaseDraft(copy(draft), copy(baseline), saved);
          baseline = saved;
          revision = current.revision ?? 0;
          draft = merged.value;
          if (merged.conflicts.length) notice = t('factory.saveConflict');
          if (!hasChanges()) return true;
          continue;
        }
        if (disposed) return true;
        const saved = options.project(record);
        const merged = rebaseDraft(copy(draft), submitted, saved, {
          draftWins: true,
        });
        baseline = saved;
        revision = record.revision ?? revision;
        exists = true;
        draft = merged.value;
        options.onSave(record);
        return true;
      }
      return false;
    } catch (cause) {
      if (!disposed) error = failureText(cause);
      return false;
    } finally {
      saving = false;
      if (!disposed && deferred) {
        const record = deferred;
        deferred = null;
        adopt(record);
      }
    }
  }

  const autosave = createDebouncedAutosave({
    getSnapshot: () => ({
      id: options.identity,
      values: options.normalize(draft),
    }),
    hasChanges,
    save: persist,
  });

  $effect(() => {
    adopt(options.record);
  });
  $effect(() => {
    if (exists || saving)
      return untrack(() =>
        options.client.registerAutosave(autosave.participant),
      );
  });
  $effect(() => {
    if (exists && hasChanges() && !saving && options.editable !== false)
      autosave.scheduleRun();
    else autosave.cancelPendingTimer();
    if (exists) options.client.notifyAutosave();
    return autosave.cancelPendingTimer;
  });
  onDestroy(() => {
    disposed = true;
    autosave.cancelPendingTimer();
  });

  function reset() {
    autosave.cancelPendingTimer();
    draft = copy(baseline);
    error = '';
    notice = '';
    options.client.notifyAutosave();
  }

  return {
    get draft() {
      return draft;
    },
    get exists() {
      return exists;
    },
    get saving() {
      return saving;
    },
    get error() {
      return error;
    },
    get notice() {
      return notice;
    },
    hasChanges,
    save: () => autosave.participant.runSave('manual', { force: !exists }),
    flush: () =>
      exists ? autosave.participant.flush() : Promise.resolve(true),
    reset,
  };
}
