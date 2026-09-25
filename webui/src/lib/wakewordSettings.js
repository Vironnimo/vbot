/**
 * Pure helpers for the Desktop Voice configuration shown in Settings → Voice.
 *
 * The Desktop status snapshot is authoritative. The panel edits a config
 * draft projected from it (`voiceConfigFromStatus`), sends only what differs
 * from the last snapshot (`buildVoiceConfigChanges`), and carries unsaved edits
 * across newer snapshots (`rebaseVoiceConfig`).
 *
 * Config shape: `{microphone, echo_cancellation, active_model_ids,
 * model_sensitivities, default_agent_id, default_session_behavior,
 * phrase_actions}`. `phrase_actions` maps a model id to
 * `{type: 'command', agent_id, session_behavior}` (null fields use the
 * defaults) or `{type: 'live_voice', mode: 'start' | 'toggle'}`; a missing
 * entry is a command with the defaults.
 */

// The server accepts at most this many Live voice wake phrases of at most
// this many characters (`live.start` `wake_phrases`).
const LIVE_WAKE_PHRASES_MAX = 8;
const LIVE_WAKE_PHRASE_MAX_CHARS = 60;
// Characters the server rejects in a wake phrase: control and format
// characters, lone surrogates, line and paragraph separators.
const WAKE_PHRASE_REJECTED = /[\p{Cc}\p{Cf}\p{Cs}\p{Zl}\p{Zp}]+/gu;

export const ACTION_CHOICE_COMMAND = 'command';
export const ACTION_CHOICE_LIVE_TOGGLE = 'live_voice:toggle';
export const ACTION_CHOICE_LIVE_START = 'live_voice:start';

const defaultCommand = () => ({
  type: 'command',
  agent_id: null,
  session_behavior: null,
});

/** The action in canonical form; anything unknown is the default command. */
function canonicalAction(action) {
  if (action?.type === 'live_voice') {
    return {
      type: 'live_voice',
      mode: action.mode === 'start' ? 'start' : 'toggle',
    };
  }
  if (action?.type === 'command') {
    return {
      type: 'command',
      agent_id: action.agent_id || null,
      session_behavior: action.session_behavior || null,
    };
  }
  return defaultCommand();
}

/** True when both actions do the same thing (missing means default command). */
export function sameVoiceAction(left, right) {
  const a = canonicalAction(left);
  const b = canonicalAction(right);
  if (a.type !== b.type) return false;
  if (a.type === 'live_voice') return a.mode === b.mode;
  return a.agent_id === b.agent_id && a.session_behavior === b.session_behavior;
}

const sameMicrophone = (left, right) =>
  left === right ||
  (left != null &&
    right != null &&
    left.index === right.index &&
    left.name === right.name &&
    left.host_api === right.host_api);

const sameList = (left, right) =>
  left.length === right.length &&
  left.every((value, index) => value === right[index]);

const cloneMicrophone = (microphone) =>
  microphone
    ? {
        index: microphone.index,
        name: microphone.name,
        host_api: microphone.host_api ?? '',
      }
    : null;

/** Project the editable Voice configuration out of a status snapshot. */
export function voiceConfigFromStatus(status) {
  const phrases = Array.isArray(status?.phrases) ? status.phrases : [];
  const sensitivities = {};
  const actions = {};
  for (const phrase of phrases) {
    if (Number.isFinite(phrase.sensitivity))
      sensitivities[phrase.model_id] = phrase.sensitivity;
    actions[phrase.model_id] = canonicalAction(phrase.action);
  }
  return {
    microphone: cloneMicrophone(status?.microphone),
    echo_cancellation: status?.echo_cancellation?.enabled !== false,
    active_model_ids: phrases.map((phrase) => phrase.model_id),
    model_sensitivities: sensitivities,
    default_agent_id: status?.default_agent_id ?? null,
    default_session_behavior:
      status?.default_session_behavior === 'new' ? 'new' : 'active',
    phrase_actions: actions,
  };
}

/** An isolated copy of a config, for snapshots and submitted drafts. */
export function cloneVoiceConfig(config) {
  return {
    ...config,
    microphone: cloneMicrophone(config.microphone),
    active_model_ids: [...config.active_model_ids],
    model_sensitivities: { ...config.model_sensitivities },
    phrase_actions: Object.fromEntries(
      Object.entries(config.phrase_actions).map(([modelId, action]) => [
        modelId,
        canonicalAction(action),
      ]),
    ),
  };
}

function rebaseEntries(draft, previous, next, same, clone) {
  const result = {};
  const keys = new Set([
    ...Object.keys(draft),
    ...Object.keys(previous),
    ...Object.keys(next),
  ]);
  for (const key of keys) {
    const edited = !same(draft[key], previous[key]);
    const value = edited ? draft[key] : next[key];
    if (value !== undefined) result[key] = clone(value);
  }
  return result;
}

/**
 * Carry the draft's unsaved edits onto a newer configuration.
 *
 * A value the user has not changed since `previous` (the configuration the
 * draft was last based on) takes the value from `next`; an edited value
 * stays. Per-phrase sensitivities and actions rebase entry by entry.
 */
export function rebaseVoiceConfig(draft, previous, next) {
  const pick = (key, same) =>
    same(draft[key], previous[key]) ? next[key] : draft[key];
  const identical = (left, right) => left === right;
  return cloneVoiceConfig({
    microphone: pick('microphone', sameMicrophone),
    echo_cancellation: pick('echo_cancellation', identical),
    active_model_ids: pick('active_model_ids', sameList),
    model_sensitivities: rebaseEntries(
      draft.model_sensitivities,
      previous.model_sensitivities,
      next.model_sensitivities,
      identical,
      (value) => value,
    ),
    default_agent_id: pick('default_agent_id', identical),
    default_session_behavior: pick('default_session_behavior', identical),
    phrase_actions: rebaseEntries(
      draft.phrase_actions,
      previous.phrase_actions,
      next.phrase_actions,
      sameVoiceAction,
      canonicalAction,
    ),
  });
}

function actionChange(action) {
  const canonical = canonicalAction(action);
  if (canonical.type === 'live_voice') return canonical;
  // The default command has no stored entry.
  if (!canonical.agent_id && !canonical.session_behavior) return null;
  const change = { type: 'command' };
  if (canonical.agent_id) change.agent_id = canonical.agent_id;
  if (canonical.session_behavior)
    change.session_behavior = canonical.session_behavior;
  return change;
}

/**
 * Build the `updateVoiceConfig` changes that turn `baseline` into `draft`;
 * an empty object when nothing differs. Sensitivities and phrase actions are
 * sent only for the phrases that changed.
 */
export function buildVoiceConfigChanges(draft, baseline) {
  const changes = {};
  if (!sameMicrophone(draft.microphone, baseline.microphone))
    changes.microphone = cloneMicrophone(draft.microphone);
  if (draft.echo_cancellation !== baseline.echo_cancellation)
    changes.echo_cancellation = draft.echo_cancellation;
  if (!sameList(draft.active_model_ids, baseline.active_model_ids))
    changes.active_model_ids = [...draft.active_model_ids];

  const sensitivities = {};
  for (const [modelId, value] of Object.entries(draft.model_sensitivities)) {
    if (
      Number.isFinite(value) &&
      value !== baseline.model_sensitivities[modelId]
    )
      sensitivities[modelId] = value;
  }
  if (Object.keys(sensitivities).length > 0)
    changes.model_sensitivities = sensitivities;

  if (draft.default_agent_id !== baseline.default_agent_id)
    changes.default_agent_id = draft.default_agent_id;
  if (draft.default_session_behavior !== baseline.default_session_behavior)
    changes.default_session_behavior = draft.default_session_behavior;

  const actions = {};
  for (const [modelId, action] of Object.entries(draft.phrase_actions)) {
    if (!sameVoiceAction(action, baseline.phrase_actions[modelId]))
      actions[modelId] = actionChange(action);
  }
  if (Object.keys(actions).length > 0) changes.phrase_actions = actions;
  return changes;
}

/** What a detection of one phrase does, with command defaults resolved. */
export function effectiveVoiceAction(config, modelId) {
  const action = canonicalAction(config.phrase_actions[modelId]);
  if (action.type === 'live_voice') return action;
  return {
    type: 'command',
    agent_id: action.agent_id ?? config.default_agent_id ?? null,
    session_behavior:
      action.session_behavior ?? config.default_session_behavior ?? 'active',
  };
}

/**
 * Active phrases that can fire on each other's words (`overlaps` from the
 * model catalog) while doing different things: a detection may then run the
 * wrong action. Returns a Map from model id to the conflicting model ids.
 */
export function overlappingPhraseConflicts(config, models) {
  const active = new Set(config.active_model_ids);
  const overlaps = new Map(
    models.map((model) => [model.id, new Set(model.overlaps ?? [])]),
  );
  const conflicts = new Map();
  const add = (modelId, otherId) => {
    if (!conflicts.has(modelId)) conflicts.set(modelId, []);
    const list = conflicts.get(modelId);
    if (!list.includes(otherId)) list.push(otherId);
  };
  const ids = [...active];
  for (const [index, left] of ids.entries()) {
    for (const right of ids.slice(index + 1)) {
      const overlap =
        overlaps.get(left)?.has(right) || overlaps.get(right)?.has(left);
      if (!overlap) continue;
      if (
        sameVoiceAction(
          effectiveVoiceAction(config, left),
          effectiveVoiceAction(config, right),
        )
      )
        continue;
      add(left, right);
      add(right, left);
    }
  }
  return conflicts;
}

/** The action select value of one phrase action. */
export function voiceActionChoice(action) {
  const canonical = canonicalAction(action);
  if (canonical.type !== 'live_voice') return ACTION_CHOICE_COMMAND;
  return canonical.mode === 'start'
    ? ACTION_CHOICE_LIVE_START
    : ACTION_CHOICE_LIVE_TOGGLE;
}

/** The phrase action an action select value stands for. */
export function voiceActionForChoice(choice, previous) {
  if (choice === ACTION_CHOICE_LIVE_START)
    return { type: 'live_voice', mode: 'start' };
  if (choice === ACTION_CHOICE_LIVE_TOGGLE)
    return { type: 'live_voice', mode: 'toggle' };
  const current = canonicalAction(previous);
  return current.type === 'command' ? current : defaultCommand();
}

function cleanWakePhrase(label) {
  if (typeof label !== 'string') return '';
  const cleaned = label
    .replace(WAKE_PHRASE_REJECTED, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return Array.from(cleaned)
    .slice(0, LIVE_WAKE_PHRASE_MAX_CHARS)
    .join('')
    .trim();
}

/**
 * The wake phrases a Live voice call should know about: the labels of the
 * active phrases that send a command to an Agent, whenever Desktop Voice is
 * enabled. The listener state and phrase problems do not matter: a phrase
 * that is not heard right now can be heard again later in the same call.
 * Empty when Voice is off.
 */
export function liveWakePhrases(status) {
  if (status?.enabled !== true) return [];
  const phrases = [];
  const seen = new Set();
  for (const phrase of status.phrases ?? []) {
    if (phrase?.effective?.type !== 'command') continue;
    const label = cleanWakePhrase(phrase.label);
    const key = label.toLowerCase();
    if (!label || seen.has(key)) continue;
    seen.add(key);
    phrases.push(label);
    if (phrases.length === LIVE_WAKE_PHRASES_MAX) break;
  }
  return phrases;
}
