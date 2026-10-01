// Pure presentation rules of the Archive in Settings: filter options and
// request filters, row facts, the retention texts, restore problem texts and
// the purge confirmation. The entries panel
// (`components/archive/ArchiveEntriesPanel.svelte`) owns requests and state;
// these helpers own only how server data reads.
import { activeLocaleTag, t } from './i18n.js';
import { formatDateTimeInApplicationZone } from './dateTimePrefs.svelte.js';

export const ARCHIVE_PAGE_SIZE = 50;
// `archive.purge` names at most this many entries per call.
export const ARCHIVE_PURGE_BATCH = 100;
// `archive.show` returns at most this many member Sessions by default.
export const ARCHIVE_SESSION_LIMIT = 100;

export const ARCHIVE_STATE_ARCHIVED = 'archived';
export const ARCHIVE_STATE_PURGING = 'purging';

// A restore that meets only these blockers succeeds under another id.
const RESTORE_CONFLICT_CODES = new Set([
  'agent_id_taken',
  'project_id_taken',
  'session_address_taken',
]);

const SCOPE_AGENT_PREFIX = 'agent:';
const SCOPE_PROJECT_PREFIX = 'project:';

function text(value) {
  return typeof value === 'string' ? value : '';
}

function formatDay(value) {
  const ms = Date.parse(text(value));
  if (!Number.isFinite(ms)) return '';
  return formatDateTimeInApplicationZone(new Date(ms), activeLocaleTag(), {
    dateStyle: 'medium',
  });
}

export function archiveKindLabel(kind) {
  switch (kind) {
    case 'agent':
      return t('archive.kind.agent');
    case 'project':
      return t('archive.kind.project');
    case 'session':
      return t('archive.kind.session');
    case 'owner_group':
      return t('archive.kind.ownerGroup');
    case 'files':
      return t('archive.kind.files');
    default:
      return text(kind);
  }
}

export function archiveKindOptions() {
  return [
    { value: '', label: t('archive.filter.allKinds') },
    { value: 'agent', label: t('archive.filter.agents') },
    { value: 'project', label: t('archive.filter.projects') },
    { value: 'session', label: t('archive.filter.sessions') },
    { value: 'owner_group', label: t('archive.filter.ownerGroups') },
    { value: 'files', label: t('archive.filter.files') },
  ];
}

// Names of live and archived Agents and Projects: live rosters first, then the
// labels of loaded Agent and Project entries for ids no longer live.
export function archiveNames({ agents = [], projects = [], entries = [] }) {
  const agentNames = new Map();
  const projectNames = new Map();
  for (const agent of agents) {
    const id = text(agent?.id);
    if (id) agentNames.set(id, text(agent.name) || id);
  }
  for (const project of projects) {
    const id = text(project?.project_id);
    if (id) projectNames.set(id, text(project.display_name) || id);
  }
  for (const entry of entries) {
    const id = text(entry?.subject_id);
    if (entry?.kind === 'agent' && id && !agentNames.has(id)) {
      agentNames.set(id, text(entry.label) || id);
    }
    if (entry?.kind === 'project' && id && !projectNames.has(id)) {
      projectNames.set(id, text(entry.label) || id);
    }
  }
  return { agentNames, projectNames };
}

// The Agent and Project choices of the scope filter: every live Identity
// Agent and Project, every one the loaded entries name, and the current
// choice, so a filter never hides its own value.
export function archiveScopeOptions({
  agentNames,
  projectNames,
  entries = [],
  scope = '',
}) {
  const agentIds = new Set(agentNames.keys());
  const projectIds = new Set(projectNames.keys());
  for (const entry of entries) {
    if (text(entry?.project_id)) projectIds.add(entry.project_id);
    else if (text(entry?.agent_id)) agentIds.add(entry.agent_id);
  }
  const current = parseArchiveScope(scope);
  if (current.agentId) agentIds.add(current.agentId);
  if (current.projectId) projectIds.add(current.projectId);
  const byLabel = (left, right) => left.label.localeCompare(right.label);
  const agentGroup = t('archive.filter.agentGroup');
  const projectGroup = t('archive.filter.projectGroup');
  const option = (prefix, id, names, group) => {
    const label = names.get(id) || id;
    return {
      value: prefix + id,
      label,
      secondaryLabel: label === id ? '' : id,
      group,
    };
  };
  return [
    { value: '', label: t('archive.filter.allScopes') },
    ...[...agentIds]
      .map((id) => option(SCOPE_AGENT_PREFIX, id, agentNames, agentGroup))
      .sort(byLabel),
    ...[...projectIds]
      .map((id) => option(SCOPE_PROJECT_PREFIX, id, projectNames, projectGroup))
      .sort(byLabel),
  ];
}

export function parseArchiveScope(scope) {
  const value = text(scope);
  if (value.startsWith(SCOPE_AGENT_PREFIX)) {
    return { agentId: value.slice(SCOPE_AGENT_PREFIX.length), projectId: '' };
  }
  if (value.startsWith(SCOPE_PROJECT_PREFIX)) {
    return {
      agentId: '',
      projectId: value.slice(SCOPE_PROJECT_PREFIX.length),
    };
  }
  return { agentId: '', projectId: '' };
}

// The `archive.list` / `archive.purge` filters of the chosen kind and scope.
export function archiveFilters({ kind = '', scope = '' } = {}) {
  return { kind: text(kind), ...parseArchiveScope(scope) };
}

export function hasArchiveFilters({ kind = '', scope = '' } = {}) {
  return Boolean(kind || scope);
}

export function isPurgeable(entry) {
  return (
    entry?.state === ARCHIVE_STATE_ARCHIVED ||
    entry?.state === ARCHIVE_STATE_PURGING
  );
}

// Whether an entry may hold folders of the user's own: files from older vBot
// versions, or a folder an older vBot moved into the Archive. Neither automatic
// deletion nor deleting every matching entry removes it; deleting it by
// itself does.
export function mayHoldUserFolders(entry) {
  return entry?.may_hold_user_folders === true || entry?.kind === 'files';
}

// An entry vBot never deletes on its own while retention is on: files from
// older vBot versions and entries that may hold the user's own folders.
export function isKeptFromRetention(entry, retentionDays) {
  return (
    Number.isInteger(retentionDays) &&
    entry?.state === ARCHIVE_STATE_ARCHIVED &&
    !entry?.purge_at
  );
}

// Whether vBot found the entry's files without a record of when they were
// archived (after restoring a data snapshot, for example).
function isRecoveredOnly(entry) {
  return entry?.origin === 'recovered' && !mayHoldUserFolders(entry);
}

export function keptFromRetentionReason(entry) {
  if (entry?.kind === 'files') return t('archive.row.neverDeletedFiles');
  if (isRecoveredOnly(entry)) return t('archive.row.neverDeletedRecovered');
  return t('archive.row.neverDeletedFolders');
}

export function sessionCountText(count) {
  if (!Number.isInteger(count) || count < 1) return '';
  return count === 1
    ? t('archive.row.oneSession')
    : t('archive.row.sessions', { count });
}

// Where an entry belonged: a Session's Agent and Project, an Extension
// group's Extension; Agents and Projects are their own scope.
export function archiveScopeText(entry, { agentNames, projectNames }) {
  if (entry?.kind === 'owner_group') return text(entry.owner_name);
  if (entry?.kind !== 'session') return '';
  const agentId = text(entry.agent_id);
  const projectId = text(entry.project_id);
  const agent = agentNames.get(agentId) || agentId;
  if (!projectId) return agent;
  return t('archive.row.agentInProject', {
    agent,
    project: projectNames.get(projectId) || projectId,
  });
}

// One row's facts: what it is, where it belonged, its Sessions, when it was
// archived and what happens next (automatic deletion or an operation).
export function archiveRow(entry, { retentionDays, agentNames, projectNames }) {
  const row = {
    id: text(entry?.entry_id),
    label: text(entry?.label) || text(entry?.subject_id),
    kind: archiveKindLabel(entry?.kind),
    scope: archiveScopeText(entry, { agentNames, projectNames }),
    sessions: sessionCountText(entry?.session_count),
    archived: t('archive.row.archivedAt', {
      date: formatDay(entry?.archived_at),
    }),
    status: '',
    statusTone: 'neutral',
    statusHint: '',
    restoreHint: '',
    purgeable: isPurgeable(entry),
  };
  switch (entry?.state) {
    case 'archiving':
      row.status = t('archive.state.archiving');
      row.statusTone = 'info';
      break;
    case 'restoring':
    case 'restored':
      row.status = t('archive.state.restoring');
      row.statusTone = 'info';
      break;
    case ARCHIVE_STATE_PURGING:
      row.status = t('archive.row.purging');
      row.statusTone = 'warn';
      break;
    default:
      if (entry?.purge_at) {
        row.status = t('archive.row.purgeAt', {
          date: formatDay(entry.purge_at),
        });
      } else if (isKeptFromRetention(entry, retentionDays)) {
        row.status = t('archive.row.neverDeleted');
        row.statusHint = keptFromRetentionReason(entry);
      }
  }
  if (entry?.restorable === false && entry?.state === ARCHIVE_STATE_ARCHIVED) {
    row.restoreHint = restoreProblemText({
      code: entry.not_restorable_reason,
    });
  }
  return row;
}

// What the Archive passes as `retentionDays` while vBot cannot read the
// period: it then deletes nothing automatically, and no entry has a date.
export const RETENTION_UNKNOWN = 'unknown';

// What an entry's detail says about its automatic deletion.
export function archiveDeletionText(entry, retentionDays, userFolders = []) {
  if (entry?.state === ARCHIVE_STATE_PURGING) return t('archive.row.purging');
  if (entry?.purge_at) {
    return t('archive.detail.purgeAt', { date: formatDay(entry.purge_at) });
  }
  if (retentionDays === RETENTION_UNKNOWN) {
    return t('archive.detail.retentionUnknown');
  }
  if (retentionDays === null) return t('archive.retention.off');
  if (!isKeptFromRetention(entry, retentionDays)) return '';
  if (entry?.kind === 'files') return t('archive.detail.neverDeletedFiles');
  if (userFolders.length === 0 && isRecoveredOnly(entry)) {
    return t('archive.detail.neverDeletedRecovered');
  }
  return userFolders.length > 0
    ? t('archive.detail.neverDeletedFolders')
    : t('archive.row.neverDeletedFolders');
}

function takenSessionIds(problem) {
  const ids = (Array.isArray(problem?.addresses) ? problem.addresses : [])
    .map((address) => text(address?.session_id))
    .filter(Boolean);
  return [...new Set(ids)].join(', ');
}

// Why another operation holds an entry ({state, path, message}): an
// interrupted restore explains itself and how to unblock it, and an entry
// being deleted never becomes restorable again.
function entryBusyText(busy) {
  const message = text(busy?.message);
  if (text(busy?.path) && message) return message;
  if (busy?.state === ARCHIVE_STATE_PURGING) {
    return t('archive.problem.entryPurging');
  }
  return t('archive.problem.entryBusy');
}

// A restore blocker or warning ({code, message, ...details}) in words. Codes
// this view does not know show the server's message.
export function restoreProblemText(problem, { warning = false } = {}) {
  const code = text(problem?.code);
  const message = text(problem?.message);
  switch (code) {
    case 'kind_not_restorable':
      return t('archive.problem.kindNotRestorable');
    case 'entry_busy':
      return entryBusyText(problem);
    case 'payload_missing':
      return t('archive.problem.payloadMissing');
    case 'payload_invalid':
      return t('archive.problem.payloadInvalid');
    case 'older_format':
      return t('archive.problem.olderFormat');
    case 'newer_format':
      return t('archive.problem.newerFormat');
    case 'agent_id_taken':
      return t('archive.problem.agentIdTaken', {
        id: text(problem.agent_id),
      });
    case 'project_id_taken':
      return t('archive.problem.projectIdTaken', {
        id: text(problem.project_id),
      });
    case 'session_address_taken':
      return t('archive.problem.sessionTaken', {
        ids: takenSessionIds(problem),
      });
    case 'invalid_target_id':
      return t('archive.problem.invalidTargetId');
    case 'project_cwd_claimed':
      return t('archive.problem.projectCwdClaimed', {
        project: text(problem.project_id),
        cwd: text(problem.cwd),
      });
    case 'scope_missing':
      if (text(problem.project_id) && text(problem.agent_id)) {
        return t('archive.problem.agentNotInTeam', {
          agent: text(problem.agent_id),
          project: text(problem.project_id),
        });
      }
      if (text(problem.project_id)) {
        return t('archive.problem.projectMissing', {
          project: text(problem.project_id),
        });
      }
      return t('archive.problem.agentMissing', {
        agent: text(problem.agent_id),
      });
    case 'workspace_path_taken':
      return warning
        ? t('archive.problem.workspaceTakenWarning', {
            path: text(problem.path),
          })
        : t('archive.problem.workspaceTaken', { path: text(problem.path) });
    case 'owner_managed':
      return t('archive.problem.ownerManaged');
    case 'grant_target_missing':
      return t('archive.problem.grantTargetMissing', {
        agent: text(problem.agent_id),
      });
    case 'root_project_missing':
      return t('archive.problem.rootProjectMissing', {
        project: text(problem.project_id),
      });
    case 'external_workspace_missing':
      return t('archive.problem.externalWorkspaceMissing', {
        path: text(problem.path),
      });
    default:
      return message || code;
  }
}

// The archived entry that holds what a `scope_missing` blocker names, to
// restore first.
export function missingScopeEntryId(problem) {
  return problem?.code === 'scope_missing' ? text(problem.entry_id) : '';
}

// Whether "Restore as" can help: the kind takes a new id and every blocker is
// a taken id or Session address.
export function canRestoreAs(entry, restoreCheck) {
  const kind = entry?.kind;
  const takesNewId =
    kind === 'agent' ||
    kind === 'project' ||
    (kind === 'session' && entry?.session_count === 1);
  if (!takesNewId || entry?.state !== ARCHIVE_STATE_ARCHIVED) return false;
  const blockers = Array.isArray(restoreCheck?.blockers)
    ? restoreCheck.blockers
    : [];
  return blockers.every((blocker) => RESTORE_CONFLICT_CODES.has(blocker.code));
}

// The id an `archive_restore_conflict` error names as taken ('' when several
// Sessions are).
export function restoreConflictId(error) {
  const conflicts = error?.details?.data?.conflicts;
  if (!Array.isArray(conflicts)) return '';
  return conflicts.map((conflict) => text(conflict?.id)).find(Boolean) ?? '';
}

export function restoreConflictText(error) {
  const id = restoreConflictId(error);
  return id
    ? t('archive.restore.conflict', { id })
    : t('archive.restore.conflictUnnamed');
}

export function restoreAsFieldLabel(kind) {
  switch (kind) {
    case 'project':
      return t('archive.restoreAs.projectLabel');
    case 'session':
      return t('archive.restoreAs.sessionLabel');
    default:
      return t('archive.restoreAs.agentLabel');
  }
}

// What the restore result means for the user: its name and warnings.
export function restoreWarningsText(result) {
  const warnings = Array.isArray(result?.warnings) ? result.warnings : [];
  return warnings
    .map((warning) => restoreProblemText(warning, { warning: true }))
    .filter(Boolean)
    .join(' ');
}

// An `archive.restore` or `archive.purge` refusal in words.
export function archiveErrorText(error) {
  const data = error?.details?.data ?? {};
  switch (error?.code) {
    case 'archive_entry_not_found':
      return t('archive.error.notFound');
    case 'archive_entry_busy':
      return entryBusyText(data);
    case 'archive_not_restorable': {
      const blockers = Array.isArray(data.blockers) ? data.blockers : [];
      const texts = blockers
        .map((blocker) => restoreProblemText(blocker))
        .filter(Boolean);
      return texts.length ? texts.join(' ') : text(error?.message);
    }
    default:
      return text(error?.message) || t('errors.generic');
  }
}

// `holdsOwnFolders`: an entry being deleted may hold folders of the user's
// own, which the deletion removes too. `kept`: how many matching entries that
// may hold such folders deleting every matching entry leaves alone.
export function purgeConfirmText({
  count,
  name = '',
  holdsOwnFolders,
  kept = 0,
}) {
  const keptText = countText(
    kept,
    () => t('archive.purgeAll.keptOne'),
    (count) => t('archive.purgeAll.keptMany', { count }),
  );
  return [deleteConfirmText({ count, name, holdsOwnFolders }), keptText]
    .filter(Boolean)
    .join(' ');
}

function deleteConfirmText({ count, name, holdsOwnFolders }) {
  if (name) {
    return holdsOwnFolders
      ? t('archive.purge.confirmOwnFolders', { name })
      : t('archive.purge.confirm', { name });
  }
  if (count === 1) {
    return holdsOwnFolders
      ? t('archive.purgeMany.confirmOneOwnFolders')
      : t('archive.purgeMany.confirmOne');
  }
  return holdsOwnFolders
    ? t('archive.purgeMany.confirmOwnFolders', { count })
    : t('archive.purgeMany.confirm', { count });
}

function listLength(value) {
  return Array.isArray(value) ? value.length : 0;
}

// One translated sentence for `count` items, or '' when there are none.
function countText(count, one, many) {
  if (count === 0) return '';
  return count === 1 ? one() : many(count);
}

// The toast after a purge: what was deleted, what vBot finishes later, and
// what stays in the Archive and why. Entries no longer in the Archive (another
// action deleted or restored them) count as done; entries that may hold the
// user's own folders, which deleting every matching entry keeps, are named
// without a warning.
export function purgeResultToast(result, { name = '' } = {}) {
  const purged = listLength(result?.purged);
  const gone = listLength(result?.gone);
  const kept = countText(
    listLength(result?.kept),
    () => t('archive.purge.keptOne'),
    (count) => t('archive.purge.keptMany', { count }),
  );
  const pending = listLength(result?.pending);
  const skipped = Array.isArray(result?.skipped) ? result.skipped : [];
  const busy = skipped.filter((entry) => entry?.reason === 'busy').length;
  const notes = [
    countText(
      pending,
      () => t('archive.purge.pendingOne'),
      (count) => t('archive.purge.pendingMany', { count }),
    ),
    countText(
      busy,
      () => t('archive.purge.busyOne'),
      (count) => t('archive.purge.busyMany', { count }),
    ),
    countText(
      skipped.length - busy,
      () => t('archive.purge.failedOne'),
      (count) => t('archive.purge.failedMany', { count }),
    ),
  ].filter(Boolean);
  const unfinished = notes.length > 0;
  let title;
  if (name && purged === 1) {
    title = t('archive.purge.success', { name });
  } else if (purged > 0) {
    title = countText(
      purged,
      () => t('archive.purge.successOne'),
      (count) => t('archive.purge.successMany', { count }),
    );
  } else if (gone > 0) {
    title = countText(
      gone,
      () => t('archive.purge.goneOne'),
      (count) => t('archive.purge.goneMany', { count }),
    );
  } else {
    title = notes.shift() ?? '';
  }
  if (kept) notes.push(kept);
  if (!title) title = notes.shift() ?? t('archive.purge.nothing');
  return {
    title,
    ...(notes.length > 0 ? { message: notes.join(' ') } : {}),
    variant: unfinished ? 'warn' : purged > 0 ? 'success' : 'info',
  };
}

// What a permanent delete did beyond moving the item to the Archive:
// `deletedText` when it is gone for good, otherwise why it is not.
export function permanentDeleteNotice(result, deletedText) {
  if (result?.purged === true) return { text: deletedText, variant: 'success' };
  if (result?.purge_pending === true) {
    return { text: t('archive.deletePending'), variant: 'warn' };
  }
  switch (result?.purge_reason) {
    case 'gone':
      return { text: t('archive.deleteGone'), variant: 'warn' };
    case 'busy':
      return { text: t('archive.deleteBusy'), variant: 'warn' };
    default:
      return { text: t('archive.deleteKept'), variant: 'warn' };
  }
}

export function treeRoleLabel(role) {
  switch (role) {
    case 'agent':
      return t('archive.detail.treeAgent');
    case 'workspace':
      return t('archive.detail.treeWorkspace');
    case 'project':
      return t('archive.detail.treeProject');
    case 'files':
      return t('archive.detail.treeFiles');
    default:
      return text(role);
  }
}

export function ownerGroupReasonText(reason) {
  switch (reason) {
    case 'extension':
      return t('archive.detail.reasonExtension');
    case 'extension_removed':
      return t('archive.detail.reasonExtensionRemoved');
    default:
      return text(reason);
  }
}
