// What the Skill history records, in words: who created and last changed an
// editable package, its last use, its revisions, and the archived packages of
// the editable scopes. The server owns the history and the archive; these
// helpers only present them.
import { activeLocaleTag, t } from '$lib/i18n.js';
import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
import { skillMergedText } from '$lib/skillMerges.js';
import { agentDisplayName } from './skillsView.js';

// The Skill page shows at most this many revisions of one Skill.
export const SKILL_HISTORY_LIMIT = 50;

const AGENT_SCOPE_PREFIX = 'agent:';

// Server timestamps carry microseconds; Date.parse needs at most milliseconds.
function parseTime(value) {
  if (typeof value !== 'string' || !value) return null;
  const ms = Date.parse(value.replace(/(\.\d{3})\d+/, '$1'));
  return Number.isFinite(ms) ? new Date(ms) : null;
}

export function formatSkillDay(value) {
  const date = parseTime(value);
  return date
    ? formatDateTimeInApplicationZone(date, activeLocaleTag(), {
        dateStyle: 'medium',
      })
    : '';
}

export function formatSkillTime(value) {
  const date = parseTime(value);
  return date
    ? formatDateTimeInApplicationZone(date, activeLocaleTag(), {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
    : '';
}

/** Who wrote a Skill or a revision: a person, an Agent, or a background job. */
export function skillActorLabel(actor) {
  switch (actor) {
    case 'human':
      return t('skills.actor.human');
    case 'agent':
      return t('skills.actor.agent');
    case 'reflection':
      return t('skills.actor.reflection');
    case 'librarian':
      return t('skills.actor.librarian');
    case 'external':
      return t('skills.actor.external');
    default:
      return t('skills.actor.unknown');
  }
}

/**
 * The provenance and use facts of a package page: who created an editable
 * package, when and by whom it changed last, and its last use (when the
 * server could count it).
 */
export function skillPageFacts(entry) {
  const facts = [];
  if (entry.editable_scope) {
    facts.push({
      id: 'created',
      label: t('skills.facts.createdBy'),
      value: skillActorLabel(entry.created_by),
    });
    const changed = formatSkillDay(entry.changed_at);
    if (changed)
      facts.push({
        id: 'changed',
        label: t('skills.facts.changed'),
        value: t('skills.facts.changedValue', {
          date: changed,
          actor: skillActorLabel(entry.changed_by),
        }),
      });
  }
  if (Number.isInteger(entry.uses)) {
    const used = entry.uses > 0 ? formatSkillDay(entry.last_used_at) : '';
    let value = t('skills.facts.neverUsed');
    if (used && entry.uses === 1)
      value = t('skills.facts.usedOnce', { date: used });
    else if (used)
      value = t('skills.facts.usedValue', { date: used, count: entry.uses });
    facts.push({ id: 'used', label: t('skills.facts.used'), value });
  }
  return facts;
}

function archiveRevisionText(revision) {
  switch (revision.reason) {
    case 'absorbed':
      return skillMergedText(
        t('skills.revision.absorbed', { target: revision.absorbed_into ?? '' }),
        revision.followed,
      );
    case 'inactive':
      return t('skills.revision.inactive');
    case 'published':
      return t('skills.revision.published');
    default:
      return t('skills.revision.deleted');
  }
}

/** What one revision did to its Skill. */
export function skillRevisionText(revision) {
  switch (revision.kind) {
    case 'create':
      return t('skills.revision.create');
    case 'external':
      return t('skills.revision.external');
    case 'archive':
      return archiveRevisionText(revision);
    case 'restore':
      return t('skills.revision.restore');
    case 'revert':
      return t('skills.revision.revert', {
        revisions: (revision.reverts ?? []).join(', '),
      });
    case 'pin':
      return t('skills.revision.pin');
    case 'unpin':
      return t('skills.revision.unpin');
    case 'baseline':
      return t('skills.revision.baseline');
    default:
      return t('skills.revision.change');
  }
}

export function skillFileChangeText(file) {
  switch (file.change) {
    case 'created':
      return t('skills.revision.fileAdded', { path: file.path });
    case 'deleted':
      return t('skills.revision.fileRemoved', { path: file.path });
    default:
      return t('skills.revision.fileChanged', { path: file.path });
  }
}

/** A baseline only records a package as found; there is nothing to revert. */
export function canRevertRevision(revision) {
  return revision.kind !== 'baseline';
}

/** The archive entry's identity across editable scopes. */
export function archivedSkillKey(item) {
  return `${item.scope}/${item.archive_id}`;
}

/** The quiet source label of an archived row. */
export function skillScopeLabel(scope) {
  return scope?.startsWith(AGENT_SCOPE_PREFIX)
    ? t('skills.source.private')
    : t('skills.library.global');
}

function skillScopeDetail(scope, agents) {
  return scope?.startsWith(AGENT_SCOPE_PREFIX)
    ? t('skills.source.privateOf', {
        name: agentDisplayName(scope.slice(AGENT_SCOPE_PREFIX.length), agents),
      })
    : t('skills.source.global');
}

/** When and why a package was archived. */
export function archivedSkillSummary(item) {
  const date = formatSkillDay(item.archived_at);
  switch (item.reason) {
    case 'absorbed':
      return t('skills.archived.absorbed', {
        target: item.absorbed_into ?? '',
        date,
      });
    case 'inactive':
      return t('skills.archived.inactive', { date });
    case 'published':
      return t('skills.archived.published', { date });
    default:
      return t('skills.archived.deleted', { date });
  }
}

/** An archived row's tooltip: its description, then where, when and who. */
export function archivedSkillDetails(item, agents = []) {
  return {
    title: item.name,
    text: item.description || '',
    rows: [
      {
        label: t('skills.details.source'),
        value: skillScopeDetail(item.scope, agents),
      },
      {
        label: t('skills.details.archived'),
        value: archivedSkillSummary(item),
      },
      {
        label: t('skills.details.archivedBy'),
        value: skillActorLabel(item.archived_by),
      },
    ],
    placement: 'pointer',
    alignTo: '.skills-row-name',
  };
}

/** Archived packages matching every search word, newest first. */
export function filterArchivedSkills(items, query, agents = []) {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return items
    .filter((item) => {
      const text = [
        item.name,
        item.description,
        item.absorbed_into,
        skillScopeLabel(item.scope),
        skillScopeDetail(item.scope, agents),
      ]
        .join(' ')
        .toLocaleLowerCase();
      return words.every((word) => text.includes(word));
    })
    .sort(
      (left, right) =>
        (right.archived_at || '').localeCompare(left.archived_at || '') ||
        left.name.localeCompare(right.name),
    );
}
