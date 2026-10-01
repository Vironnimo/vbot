import { beforeEach, describe, expect, it } from 'vitest';

import {
  archiveDeletionText,
  archiveRow,
  canRestoreAs,
  permanentDeleteNotice,
  purgeResultToast,
  restoreProblemText,
  retentionNotice,
  RETENTION_UNKNOWN,
} from '../archiveView.js';
import { init, t } from '../i18n.js';

const NAMES = { agentNames: new Map(), projectNames: new Map() };

function entry(fields) {
  return {
    entry_id: 'e1',
    kind: 'agent',
    state: 'archived',
    subject_id: 'coder',
    label: 'Coder',
    archived_at: '2026-09-20T10:00:00+00:00',
    purge_at: null,
    session_count: 1,
    restorable: true,
    ...fields,
  };
}

describe('archive view rules', () => {
  beforeEach(() => init('en'));

  it.each([
    [
      'an entry being archived',
      { state: 'archiving' },
      30,
      'archive.state.archiving',
      false,
    ],
    [
      'an entry being restored',
      { state: 'restoring' },
      30,
      'archive.state.restoring',
      false,
    ],
    [
      'an entry being deleted',
      { state: 'purging' },
      30,
      'archive.row.purging',
      true,
    ],
    ['an entry kept from deletion', {}, 30, 'archive.row.neverDeleted', true],
    ['any entry while deletion is off', {}, null, '', true],
    // Not "never deleted": the period is unknown, so nothing is deleted for now.
    ['any entry while the period is unknown', {}, RETENTION_UNKNOWN, '', true],
  ])(
    'states what happens next to %s',
    (_name, fields, days, key, purgeable) => {
      const row = archiveRow(entry(fields), { retentionDays: days, ...NAMES });
      expect(row.status).toBe(key ? t(key) : '');
      expect(row.purgeable).toBe(purgeable);
    },
  );

  it.each([
    [
      'older files',
      { kind: 'files', may_hold_user_folders: true },
      'archive.row.neverDeletedFiles',
      'archive.detail.neverDeletedFiles',
    ],
    [
      'an entry that may hold folders of the user',
      { may_hold_user_folders: true },
      'archive.row.neverDeletedFolders',
      'archive.row.neverDeletedFolders',
    ],
    [
      'an entry found without its record',
      { origin: 'recovered' },
      'archive.row.neverDeletedRecovered',
      'archive.detail.neverDeletedRecovered',
    ],
  ])(
    'says why retention never deletes %s',
    (_name, fields, rowKey, detailKey) => {
      const kept = entry(fields);
      const row = archiveRow(kept, { retentionDays: 30, ...NAMES });
      expect(row.statusHint).toBe(t(rowKey));
      expect(archiveDeletionText(kept, 30)).toBe(t(detailKey));
    },
  );

  it.each([
    [null, 'archive.retention.off', 'archive.retention.off'],
    [
      RETENTION_UNKNOWN,
      'archive.retention.unknown',
      'archive.detail.retentionUnknown',
    ],
  ])(
    'says what retention does while the period is %s',
    (days, noticeKey, detailKey) => {
      expect(retentionNotice(days)).toBe(t(noticeKey));
      expect(archiveDeletionText(entry({}), days)).toBe(t(detailKey));
    },
  );

  it.each([
    ['an Agent', { kind: 'agent' }, null, true],
    ['a single Session', { kind: 'session', session_count: 1 }, null, true],
    ['a Session batch', { kind: 'session', session_count: 3 }, null, false],
    ['Extension Sessions', { kind: 'owner_group' }, null, false],
    [
      'an Agent whose ID is taken',
      { kind: 'agent' },
      { blockers: [{ code: 'agent_id_taken', agent_id: 'coder' }] },
      true,
    ],
    [
      'an Agent missing its files',
      { kind: 'agent' },
      { blockers: [{ code: 'payload_missing' }] },
      false,
    ],
  ])(
    'offers Restore as for %s only when a new ID helps',
    (_name, fields, check, expected) => {
      expect(canRestoreAs(entry(fields), check)).toBe(expected);
    },
  );

  it('words known restore problems with their facts and shows the server message for others', () => {
    expect(
      restoreProblemText({ code: 'scope_missing', project_id: 'vbot' }),
    ).toBe(t('archive.problem.projectMissing', { project: 'vbot' }));
    expect(
      restoreProblemText(
        { code: 'workspace_path_taken', path: '/w/coder' },
        { warning: true },
      ),
    ).toBe(t('archive.problem.workspaceTakenWarning', { path: '/w/coder' }));
    expect(
      restoreProblemText({ code: 'future_problem', message: 'Server says no' }),
    ).toBe('Server says no');
  });

  it.each([
    [
      'what was deleted and what vBot finishes later',
      { purged: [{}, {}], pending: [{ reason: 'stopped' }] },
      () => ({
        title: t('archive.purge.successMany', { count: 2 }),
        message: t('archive.purge.pendingOne'),
        variant: 'warn',
      }),
    ],
    [
      'entries another action already removed as done',
      { purged: [], gone: ['e1', 'e2'] },
      () => ({
        title: t('archive.purge.goneMany', { count: 2 }),
        variant: 'info',
      }),
    ],
    [
      'entries kept because another action holds them or the delete failed',
      {
        skipped: [
          { entry_id: 'e1', reason: 'busy', state: 'restoring' },
          { entry_id: 'e2', reason: 'OSError', state: 'archived' },
        ],
      },
      () => ({
        title: t('archive.purge.busyOne'),
        message: t('archive.purge.failedOne'),
        variant: 'warn',
      }),
    ],
    [
      'entries kept because they may hold folders of the user, without a warning',
      { purged: [], kept: [{ entry_id: 'e1', reason: 'files' }] },
      () => ({ title: t('archive.purge.keptOne'), variant: 'info' }),
    ],
    [
      'that nothing was deleted',
      {},
      () => ({ title: t('archive.purge.nothing'), variant: 'info' }),
    ],
  ])('words a purge result: %s', (_name, result, expected) => {
    expect(purgeResultToast(result)).toEqual(expected());
  });

  it.each([
    [{ purged: true }, 'Gone for good', 'success'],
    [{ purge_pending: true, purge_reason: 'stopped' }, 'archive.deletePending'],
    [{ purge_reason: 'gone' }, 'archive.deleteGone'],
    [{ purge_reason: 'busy' }, 'archive.deleteBusy'],
    [{ purge_reason: 'usage_import_failed' }, 'archive.deleteKept'],
  ])(
    'says what a permanent delete did for %o',
    (result, text, variant = 'warn') => {
      expect(permanentDeleteNotice(result, 'Gone for good')).toEqual({
        text: text === 'Gone for good' ? text : t(text),
        variant,
      });
    },
  );
});
