import { t } from '$lib/i18n.js';
import { skillMergedText } from '$lib/skillMerges.js';
import { formatAbsoluteTime } from '$lib/timeText.js';
import { isPlainObject } from '$lib/values.js';

// Reflection reviews execute in a same-Agent fork whose lifecycle events carry
// the reviewed source session. These helpers map the run-kind vocabulary onto
// Activity-panel presentation, project the per-source tracking entries
// written by chatRunStream into sortable panel rows, and word what a finished
// review changed in Memory and Skills.
const REFLECTION_RUN_KIND_SCOPES = {
  memory_reflection: 'memory',
  skill_reflection: 'skill',
  reflection: 'combined',
};

// A Memory entry quoted in a refusal stays one readable line.
const QUOTED_ENTRY_CHARACTERS = 120;

export const isReflectionRunKind = (runKind) =>
  typeof runKind === 'string' && runKind in REFLECTION_RUN_KIND_SCOPES;

const reflectionScopeForRunKind = (runKind) =>
  isReflectionRunKind(runKind) ? REFLECTION_RUN_KIND_SCOPES[runKind] : '';

const isCount = (value) => Number.isInteger(value) && value >= 0;

// What a finished review changed, as the server counts it from the Memory
// and Skill histories; null when the row carries no outcome.
const rowOutcome = (outcome) =>
  isPlainObject(outcome) && isCount(outcome.memory) && isCount(outcome.skills)
    ? {
        memory: outcome.memory,
        skills: outcome.skills,
        undone: outcome.undone === true,
      }
    : null;

const rowDetails = (details) => {
  const source = isPlainObject(details) ? details : {};
  return {
    changes: Array.isArray(source.changes) ? source.changes : null,
    loading: source.loading === true,
    loadError: typeof source.loadError === 'string' ? source.loadError : '',
    undoing: source.undoing === true,
    undoError: reflectionUndoErrorText(source.undoError),
  };
};

export const reflectionTaskRows = (sessionState) => {
  const entries = isPlainObject(sessionState?.reflectionTasks)
    ? sessionState.reflectionTasks
    : {};
  const details = isPlainObject(sessionState?.reflectionDetails)
    ? sessionState.reflectionDetails
    : {};
  return Object.entries(entries)
    .filter(
      ([, entry]) =>
        isPlainObject(entry) &&
        typeof entry.sessionId === 'string' &&
        entry.sessionId.length > 0,
    )
    .map(([runId, entry]) => ({
      runId,
      sessionId: entry.sessionId,
      runKind: entry.runKind,
      scope: reflectionScopeForRunKind(entry.runKind),
      status:
        typeof entry.status === 'string' && entry.status
          ? entry.status
          : 'running',
      startedAt: typeof entry.startedAt === 'string' ? entry.startedAt : '',
      outcome: rowOutcome(entry.outcome),
      details: rowDetails(details[runId]),
    }))
    .sort((left, right) => {
      const activeDifference =
        Number(right.status === 'running') - Number(left.status === 'running');
      return (
        activeDifference ||
        (Date.parse(right.startedAt) || 0) - (Date.parse(left.startedAt) || 0)
      );
    });
};

// Coarse elapsed time for a running review; the panel re-renders it from a
// ticking clock only while the panel is open with running reflections.
export const reflectionElapsedLabel = (startedAt, nowMs) => {
  const startedMs = Date.parse(startedAt);
  if (Number.isNaN(startedMs) || !Number.isFinite(nowMs)) {
    return '';
  }
  const elapsedMs = Math.max(0, nowMs - startedMs);
  if (elapsedMs < 60_000) {
    return t('chat.activity.reflectionElapsedSeconds', {
      count: Math.floor(elapsedMs / 1000),
    });
  }
  return t('chat.activity.reflectionElapsedMinutes', {
    count: Math.floor(elapsedMs / 60_000),
  });
};

// Whether a finished review left anything to list and take back.
export const reflectionHasChanges = (outcome) =>
  Boolean(outcome) && outcome.memory + outcome.skills > 0;

// The quiet one-line summary of a finished review.
export const reflectionOutcomeLabel = (outcome) => {
  if (!outcome) {
    return '';
  }
  if (outcome.undone) {
    return t('chat.activity.outcome.undone');
  }
  const memory =
    outcome.memory === 1
      ? t('chat.activity.outcome.memoryOne')
      : t('chat.activity.outcome.memoryMany', { count: outcome.memory });
  const skills =
    outcome.skills === 1
      ? t('chat.activity.outcome.skillOne')
      : t('chat.activity.outcome.skillMany', { count: outcome.skills });
  if (outcome.memory > 0 && outcome.skills > 0) {
    return t('chat.activity.outcome.both', { memory, skills });
  }
  if (outcome.memory > 0) {
    return t('chat.activity.outcome.one', { changes: memory });
  }
  if (outcome.skills > 0) {
    return t('chat.activity.outcome.one', { changes: skills });
  }
  return t('chat.activity.outcome.nothing');
};

const memoryScopeLabel = (scope) =>
  scope === 'user'
    ? t('chat.activity.change.userMemory')
    : t('chat.activity.change.agentMemory');

const changeKindLabel = (kind) => {
  switch (kind) {
    case 'added':
      return t('chat.activity.change.added');
    case 'replaced':
      return t('chat.activity.change.replaced');
    case 'removed':
      return t('chat.activity.change.removed');
    case 'created':
      return t('chat.activity.change.created');
    case 'changed':
      return t('chat.activity.change.changed');
    case 'archived':
      return t('chat.activity.change.archived');
    case 'file_written':
      return t('chat.activity.change.fileWritten');
    case 'file_removed':
      return t('chat.activity.change.fileRemoved');
    default:
      return t('chat.activity.change.other');
  }
};

const stringList = (value) =>
  Array.isArray(value)
    ? value.filter((item) => typeof item === 'string' && item)
    : [];

// One line per change the review made, in the server's order: what happened,
// where (a Memory scope or a Skill name), and the entry text or the files.
export const reflectionChangeItems = (changes) =>
  (Array.isArray(changes) ? changes : [])
    .filter((change) => isPlainObject(change))
    .map((change, index) => {
      const memory = change.store === 'memory';
      const absorbedInto =
        typeof change.absorbed_into === 'string' ? change.absorbed_into : '';
      return {
        key: `${change.store}:${index}`,
        kind: changeKindLabel(change.kind),
        place: memory
          ? memoryScopeLabel(change.scope)
          : typeof change.skill === 'string'
            ? change.skill
            : '',
        detail: memory
          ? typeof change.text === 'string'
            ? change.text
            : ''
          : absorbedInto
            ? skillMergedText(
                t('chat.activity.change.mergedInto', { skill: absorbedInto }),
                change.followed,
              )
            : stringList(change.files).join(', '),
        mono: !memory,
        undone: change.undone === true,
      };
    });

// Who made the later change that blocks an undo, in the words of a sentence
// ending "was changed again later by ...". Memory names a person `rpc` and a
// Tool `tool`; a Tool call of a background review counts as that review.
const laterActorLabel = (later) => {
  if (isReflectionRunKind(later?.run_kind)) {
    return t('chat.activity.later.review');
  }
  switch (later?.actor) {
    case 'rpc':
    case 'human':
      return t('chat.activity.later.you');
    case 'tool':
    case 'agent':
      return t('chat.activity.later.agent');
    case 'reflection':
      return t('chat.activity.later.review');
    case 'librarian':
      return t('chat.activity.later.maintenance');
    case 'internal':
      return t('chat.activity.later.vbot');
    case 'external':
      return t('chat.activity.later.external');
    default:
      return t('chat.activity.later.unknown');
  }
};

const quotedEntry = (text) => {
  const value = typeof text === 'string' ? text : '';
  return value.length > QUOTED_ENTRY_CHARACTERS
    ? `${value.slice(0, QUOTED_ENTRY_CHARACTERS - 1)}…`
    : value;
};

// The user-facing reason an undo failed. A refusal over a later change names
// that change; any other failure keeps the server's own message.
export function reflectionUndoErrorText(error) {
  if (!isPlainObject(error)) {
    return '';
  }
  const conflict = isPlainObject(error.conflict) ? error.conflict : null;
  if (!conflict) {
    return typeof error.message === 'string' ? error.message : '';
  }
  const later = isPlainObject(conflict.later) ? conflict.later : null;
  const actor = later ? laterActorLabel(later) : '';
  const time = later ? formatAbsoluteTime(later.at) || String(later.at) : '';
  if (conflict.store === 'memory') {
    const scope = memoryScopeLabel(conflict.scope);
    const text = quotedEntry(conflict.text);
    return later
      ? t('chat.activity.undoConflict.memoryBy', { scope, text, actor, time })
      : t('chat.activity.undoConflict.memory', { scope, text });
  }
  const skill = typeof conflict.skill === 'string' ? conflict.skill : '';
  return later
    ? t('chat.activity.undoConflict.skillBy', { skill, actor, time })
    : t('chat.activity.undoConflict.skill', { skill });
}
