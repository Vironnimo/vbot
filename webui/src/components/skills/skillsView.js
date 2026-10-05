import { t } from '$lib/i18n.js';

// Presentation only: the server owns availability, identity, and write scopes.
export const SKILL_PAGE_SIZE = 50;

export function agentDisplayName(agentId, agents) {
  return agents.find((agent) => agent.id === agentId)?.name || agentId;
}

function projectDisplayName(entry, projects) {
  return (
    projects.find((project) => project.project_id === entry.project_id)?.name ||
    entry.origin?.slice(8) ||
    entry.project_id
  );
}

// A global root's folder name (an Extension or a configured folder) as a
// label: `computer_use` → `Computer use`.
export function humanizeSourceLabel(label) {
  const text = String(label ?? '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return text ? text[0].toLocaleUpperCase() + text.slice(1) : '';
}

/** The quiet source label of a library row. */
export function skillSourceLabel(entry) {
  if (entry.owner_id) return t('skills.source.private');
  if (entry.origin?.startsWith('project:')) return t('skills.source.project');
  if (entry.origin === 'bundled') return t('skills.library.bundled');
  return humanizeSourceLabel(entry.source_label) || t('skills.library.global');
}

/** Where a package lives, spelled out for the detail pane. */
export function skillSourceDetail(entry, agents = [], projects = []) {
  if (entry.owner_id)
    return t('skills.source.privateOf', {
      name: agentDisplayName(entry.owner_id, agents),
    });
  if (entry.origin?.startsWith('project:'))
    return t('skills.source.projectOf', {
      name: projectDisplayName(entry, projects),
    });
  if (entry.origin === 'bundled') return t('skills.source.bundled');
  const label = humanizeSourceLabel(entry.source_label);
  return label
    ? t('skills.source.globalFrom', { name: label })
    : t('skills.source.global');
}

/** A package as "the … copy" when several packages share its name. */
export function skillCopyLabel(entry, agents = [], projects = []) {
  if (entry.owner_id)
    return t('skills.copy.private', {
      name: agentDisplayName(entry.owner_id, agents),
    });
  if (entry.origin?.startsWith('project:'))
    return t('skills.copy.project', {
      name: projectDisplayName(entry, projects),
    });
  if (entry.origin === 'bundled') return t('skills.copy.bundled');
  const label = humanizeSourceLabel(entry.source_label);
  return label
    ? t('skills.copy.source', { name: label })
    : t('skills.copy.global');
}

/** The number of Skills an Agent or Project projection currently activates. */
export function activeSkillCount(projection) {
  return (projection?.skills ?? []).filter((row) =>
    'active' in row
      ? row.active
      : ['own', 'project', 'allowed'].includes(row.grant),
  ).length;
}

export const LIBRARY_SCOPES = ['all', 'global', 'bundled', 'shared'];

// The collection of deleted packages kept in the editable scopes' archives.
export const ARCHIVED_COLLECTION = 'archived';

export function matchesSkillScope(entry, scope) {
  if (scope === 'all') return true;
  if (scope === 'shared') return Boolean(entry.shared);
  return entry.origin === scope;
}

function libraryLabel(scope) {
  switch (scope) {
    case 'global':
      return t('skills.library.global');
    case 'bundled':
      return t('skills.library.bundled');
    case 'shared':
      return t('skills.library.shared');
    default:
      return t('skills.library.all');
  }
}

/**
 * Sidebar collections: the library filters of the package list and the
 * archived packages, then one entry per Identity Agent and Project whose count
 * is the Skills it currently activates.
 */
export function skillCollections(
  entries,
  agents = [],
  projects = [],
  archived = [],
) {
  return [
    ...LIBRARY_SCOPES.map((key) => ({
      key,
      label: libraryLabel(key),
      section: 'library',
      count: entries.filter((entry) => matchesSkillScope(entry, key)).length,
    })),
    {
      key: ARCHIVED_COLLECTION,
      label: t('skills.library.archived'),
      section: 'library',
      count: archived.length,
    },
    ...agents.map((agent) => ({
      key: `agent:${agent.id}`,
      id: agent.id,
      label: agent.name || agent.id,
      section: 'agents',
      count: activeSkillCount(agent),
    })),
    ...projects.map((project) => ({
      key: `project:${project.project_id}`,
      id: project.project_id,
      label: project.name || project.project_id,
      section: 'projects',
      count: activeSkillCount(project),
    })),
  ];
}

/** Title, subtitle and empty state of a collection. */
export function skillCollectionText(collection) {
  const key = collection?.section === 'library' ? collection.key : null;
  switch (collection?.section) {
    case 'agents':
      return {
        subtitle: t('skills.subtitle.agent', { name: collection.label }),
        empty: t('skills.empty.agent'),
        emptyHelp: t('skills.empty.agentHelp'),
      };
    case 'projects':
      return {
        subtitle: t('skills.subtitle.project', { name: collection.label }),
        empty: t('skills.empty.project'),
        emptyHelp: t('skills.empty.projectHelp'),
      };
    default:
      break;
  }
  switch (key) {
    case 'global':
      return {
        subtitle: t('skills.subtitle.global'),
        empty: t('skills.empty.global'),
        emptyHelp: t('skills.empty.globalHelp'),
      };
    case 'bundled':
      return {
        subtitle: t('skills.subtitle.bundled'),
        empty: t('skills.empty.bundled'),
        emptyHelp: t('skills.empty.bundledHelp'),
      };
    case 'shared':
      return {
        subtitle: t('skills.subtitle.shared'),
        empty: t('skills.empty.shared'),
        emptyHelp: t('skills.empty.sharedHelp'),
      };
    case ARCHIVED_COLLECTION:
      return {
        subtitle: t('skills.subtitle.archived'),
        empty: t('skills.empty.archived'),
        emptyHelp: t('skills.empty.archivedHelp'),
      };
    default:
      return {
        subtitle: t('skills.subtitle.all'),
        empty: t('skills.empty.all'),
        emptyHelp: t('skills.empty.allHelp'),
      };
  }
}

export function filterSkills(
  entries,
  query,
  scope = 'all',
  status = 'all',
  agents = [],
) {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return entries
    .filter((entry) => {
      if (!matchesSkillScope(entry, scope)) return false;
      if (status === 'attention') {
        if (
          !skillDiagnosticLines(entry).length &&
          !['unavailable', 'invalid'].includes(entry.status)
        )
          return false;
      } else if (status !== 'all' && entry.status !== status) return false;
      const text = [
        entry.name,
        entry.description,
        entry.origin,
        skillSourceLabel(entry),
        entry.owner_id,
        entry.owner_id ? agentDisplayName(entry.owner_id, agents) : '',
      ]
        .join(' ')
        .toLocaleLowerCase();
      return words.every((word) => text.includes(word));
    })
    .sort(
      (left, right) =>
        left.name.localeCompare(right.name) ||
        (left.id || '').localeCompare(right.id || ''),
    );
}

// Only the presentation omits YAML; the Original text tab and editor retain it.
export function skillInstructionBody(content) {
  return content.replace(
    /^\uFEFF?---[^\S\r\n]*\r?\n[\s\S]*?\r?\n---[^\S\r\n]*(?:\r?\n|$)/,
    '',
  );
}

export function skillStatusVariant(entry) {
  switch (entry.status) {
    case 'available':
      return 'success';
    case 'unavailable':
      return 'warn';
    case 'disabled':
      return 'neutral';
    default:
      return 'error';
  }
}

export function skillStatusLabel(entry) {
  switch (entry.status) {
    case 'available':
      return t('skills.status.available');
    case 'unavailable':
      return t('skills.status.unavailable');
    case 'disabled':
      return t('skills.status.disabled');
    default:
      return t('skills.status.invalid');
  }
}

// A row's diagnostics disclosure content: requirement reasons plus validation
// warnings. Empty when there is nothing to explain.
export function skillDiagnosticLines(entry) {
  const missing = Array.isArray(entry.missing) ? entry.missing : [];
  const optional = Array.isArray(entry.optional_missing)
    ? entry.optional_missing
    : [];
  const warnings = Array.isArray(entry.warnings) ? entry.warnings : [];
  return [...missing, ...optional, ...warnings];
}

export function createSkillDocument(name, description, instructions) {
  return `---\nname: ${JSON.stringify(name.trim())}\ndescription: ${JSON.stringify(description.trim())}\n---\n\n${instructions}`;
}

// Why a package without `editable_scope` cannot be edited or deleted, by
// its kind of source.
function readOnlyCase(entry) {
  if (entry.status === 'invalid') return 'invalid';
  return ['bundled', 'extension', 'folder', 'project'].includes(
    entry.source_kind,
  )
    ? entry.source_kind
    : 'other';
}

/** Why a package is read only (`editable_scope` is empty), in full. */
export function skillReadOnlyReason(entry) {
  const name = humanizeSourceLabel(entry.source_label);
  switch (readOnlyCase(entry)) {
    case 'invalid':
      return t('skills.readOnlyReason.invalid');
    case 'bundled':
      return t('skills.readOnlyReason.bundled');
    case 'extension':
      return t('skills.readOnlyReason.extension', { name });
    case 'folder':
      return t('skills.readOnlyReason.folder', { name });
    case 'project':
      return t('skills.readOnlyReason.project');
    default:
      return t('skills.readOnlyReason.other');
  }
}

/** The same reason as a short hint beside a disabled menu item. */
export function skillReadOnlyHint(entry) {
  const name = humanizeSourceLabel(entry.source_label);
  switch (readOnlyCase(entry)) {
    case 'invalid':
      return t('skills.readOnlyHint.invalid');
    case 'bundled':
      return t('skills.readOnlyHint.bundled');
    case 'extension':
      return t('skills.readOnlyHint.extension', { name });
    case 'folder':
      return t('skills.readOnlyHint.folder', { name });
    case 'project':
      return t('skills.readOnlyHint.project');
    default:
      return t('skills.readOnlyHint.other');
  }
}

/**
 * The delete confirmation of an editable package: who loses it (every Agent
 * for a global package, the owner and its share receivers for a private one)
 * and that it moves to Archived.
 */
export function skillDeleteConfirmText(entry, agents = []) {
  if (!entry.owner_id)
    return t('skills.deleteGlobalConfirm', { name: entry.name });
  const owner = agentDisplayName(entry.owner_id, agents);
  return entry.shared_with?.length
    ? t('skills.deleteSharedConfirm', { name: entry.name, owner })
    : t('skills.deletePrivateConfirm', { name: entry.name, owner });
}

/** What a collection's count counts. */
export function skillCollectionCountText(collection) {
  switch (collection?.section) {
    case 'agents':
      return t('skills.collectionCount.agent', { count: collection.count });
    case 'projects':
      return t('skills.collectionCount.project', { count: collection.count });
    default:
      return collection?.key === ARCHIVED_COLLECTION
        ? t('skills.collectionCount.archived', { count: collection.count })
        : t('skills.collectionCount.library', { count: collection?.count });
  }
}
