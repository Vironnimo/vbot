import { t } from './i18n.js';
import { RPC_ERROR_NETWORK, RPC_ERROR_RESPONSE } from './apiErrors.js';

export class InterruptedSettingsSave extends Error {
  constructor(cause, reconcile) {
    super(cause.message, { cause });
    this.reconcile = reconcile;
  }
}

export function interruptedWrite(error) {
  return [RPC_ERROR_NETWORK, RPC_ERROR_RESPONSE].includes(error?.code);
}

// `settings.update` refuses a write based on values that changed meanwhile.
export const SETTINGS_CONFLICT_CODE = 'settings_conflict';
// A conflict re-reads Settings and retries; only a stream of concurrent
// writers to the same section exhausts this.
const MAX_SAVE_ATTEMPTS = 3;

export function isSettingsConflict(error) {
  return error?.code === SETTINGS_CONFLICT_CODE;
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function plain(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function sameValue(left, right) {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((item, index) => sameValue(item, right[index]))
    );
  }
  if (!isPlainObject(left) || !isPlainObject(right)) return false;
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  return [...keys].every((key) => sameValue(left[key], right[key]));
}

function mergeLeaves(draft, origin, saved, path, options) {
  // If a lost response belonged to a committed write, later local edits
  // derive from that submitted value, including an edit back to the old value.
  if (options.submitted !== undefined) {
    let submitted = options.submitted;
    for (const key of path) submitted = submitted?.[key];
    if (
      sameValue(saved, submitted) ||
      options.equivalentSubmission?.(path, saved)
    )
      return plain(draft);
    if (
      path.length < options.leafDepth &&
      isPlainObject(draft) &&
      isPlainObject(saved) &&
      isPlainObject(origin)
    ) {
      return mergeObjects(draft, origin, saved, path, options);
    }
  }
  if (sameValue(draft, origin)) return plain(saved);
  if (sameValue(saved, origin) || sameValue(saved, draft)) return plain(draft);
  if (
    path.length < options.leafDepth &&
    isPlainObject(draft) &&
    isPlainObject(origin) &&
    isPlainObject(saved)
  ) {
    return mergeObjects(draft, origin, saved, path, options);
  }
  if (options.draftWins) return plain(draft);
  options.conflicts.push(path.join('.'));
  return plain(saved);
}

function mergeObjects(draft, origin, saved, path, options) {
  const merged = {};
  for (const key of new Set([...Object.keys(draft), ...Object.keys(saved)])) {
    const value = mergeLeaves(
      draft[key],
      origin[key],
      saved[key],
      [...path, key],
      options,
    );
    if (value !== undefined) merged[key] = value;
  }
  return merged;
}

/**
 * Three-way merge of a local draft onto newer saved values, leaf by leaf.
 *
 * A leaf the draft left at `origin` takes the saved value; a leaf only the
 * draft changed keeps the draft. A leaf both changed differently is a
 * conflict: the saved value wins unless `draftWins` is set. Objects merge per
 * key; arrays and scalars are leaves, and so is every value `leafDepth` keys
 * deep, for a value whose parts are only valid together.
 *
 * @returns {{ value: unknown, conflicts: string[] }}
 */
export function rebaseDraft(
  draft,
  origin,
  saved,
  {
    draftWins = false,
    leafDepth = Infinity,
    submitted,
    equivalentSubmission,
  } = {},
) {
  const options = {
    draftWins,
    leafDepth,
    submitted,
    equivalentSubmission,
    conflicts: [],
  };
  const value = mergeLeaves(draft, origin, saved, [], options);
  return { value, conflicts: options.conflicts };
}

/**
 * A Settings editor's local draft plus the saved values it was derived from.
 *
 * `save()` sends the draft through the supplied `submit` callback together
 * with its origin as `base`, so the server refuses the write when a value it
 * would change was changed meanwhile (another editor, window, or a save that
 * finished after the user left). The draft is then rebased onto Settings read
 * again through `reload`: untouched values take the saved ones, a value
 * changed on both sides takes the saved one and reports it, and the remaining
 * edits are retried. After a successful write, values edited while it ran
 * stay; the rest take the saved values, so normalization reaches the visible
 * draft.
 *
 * @param {object} params
 * @param {object} params.settings - The Settings the draft was seeded from.
 * @param {(settings: object) => object} params.fromSettings - Settings -> draft values.
 * @param {() => object} params.read - Reads the current draft.
 * @param {(values: object) => void} params.write - Replaces the draft.
 * @param {(values: object, origin?: object) => object} params.toPayload - Draft
 *   values -> update sections. `toPayload(draft, origin)` builds the write, so
 *   it may name only what changed; `toPayload(origin)` builds the `base`.
 * @param {(params: object) => Promise<object>} params.submit - Sends the
 *   sections plus `base` and resolves to the saved Settings.
 * @param {() => Promise<object>} params.reload - Re-reads saved Settings after a conflict.
 * @param {number} [params.leafDepth] - Draft depth at which a value merges as
 *   a whole (see `rebaseDraft`).
 */
export function createSettingsDraft({
  settings,
  fromSettings,
  read,
  write,
  toPayload,
  submit,
  reload,
  leafDepth = Infinity,
  recoverOnReconnect = false,
}) {
  if (typeof submit !== 'function' || typeof reload !== 'function')
    throw new TypeError('Settings submit and reload callbacks are required');
  let origin = plain(fromSettings(settings));
  let unconfirmed;

  function replaceDraft(value) {
    if (!sameValue(value, read())) write(value);
  }

  function adopt(nextSettings) {
    const saved = plain(fromSettings(nextSettings));
    const submittedPayload = unconfirmed && toPayload(unconfirmed);
    // Editors can submit the string "6" as the number 6 (or an empty field
    // as its default). Compare through the editor's wire projection, also
    // when another window changed a different leaf.
    function equivalentSubmission(path, value) {
      if (!path.length) return sameValue(toPayload(value), submittedPayload);
      const candidate = plain(unconfirmed);
      let parent = candidate;
      for (const key of path.slice(0, -1)) {
        if (!isPlainObject(parent?.[key])) return false;
        parent = parent[key];
      }
      parent[path.at(-1)] = value;
      return sameValue(toPayload(candidate), submittedPayload);
    }
    const { value, conflicts } = rebaseDraft(read(), origin, saved, {
      leafDepth,
      submitted: unconfirmed,
      equivalentSubmission,
    });
    origin = saved;
    replaceDraft(value);
    return conflicts.length > 0;
  }

  function settle(submitted, nextSettings) {
    const saved = plain(fromSettings(nextSettings));
    const { value } = rebaseDraft(read(), submitted, saved, {
      draftWins: true,
      leafDepth,
    });
    origin = saved;
    replaceDraft(value);
  }

  /**
   * Run the shared save lifecycle: mark saving, clear the error, write, and
   * commit the returned Settings; failures surface through `onError`.
   *
   * @param {object} params
   * @param {(next: object) => void} params.onCommit - Receives the saved Settings.
   * @param {(message: string) => void} params.onError - Sets/clears the error text.
   * @param {(saving: boolean) => void} params.setSaving - Drives the saving flag.
   * @returns {Promise<boolean>} Whether the draft is saved.
   */
  async function save({ onCommit, onError, setSaving }) {
    setSaving(true);
    onError('');
    let conflicted = false;

    try {
      for (let attempt = 1; ; attempt += 1) {
        const submitted = plain(read());
        let nextSettings;
        try {
          nextSettings = await submit({
            ...toPayload(submitted, origin),
            base: toPayload(origin),
          });
        } catch (error) {
          if (recoverOnReconnect && interruptedWrite(error))
            unconfirmed = submitted;
          if (!isSettingsConflict(error) || attempt >= MAX_SAVE_ATTEMPTS) {
            throw error;
          }
          const currentSettings = await reload();
          conflicted = adopt(currentSettings) || conflicted;
          onCommit(currentSettings);
          if (sameValue(read(), origin)) break;
          continue;
        }
        settle(submitted, nextSettings);
        unconfirmed = undefined;
        onCommit(nextSettings);
        break;
      }
      if (conflicted) onError(t('settings.saveConflict'));
      return true;
    } catch (error) {
      onError(`${t('settings.saveError')} ${error.message}`);
      if (recoverOnReconnect && interruptedWrite(error)) {
        const reconcile = async () => {
          const saved = await reload();
          const conflict = adopt(saved);
          unconfirmed = undefined;
          onCommit(saved);
          onError(conflict ? t('settings.saveConflict') : '');
          return !sameValue(read(), origin);
        };
        throw new InterruptedSettingsSave(error, reconcile);
      }
      return false;
    } finally {
      setSaving(false);
    }
  }

  return { save, adopt };
}
