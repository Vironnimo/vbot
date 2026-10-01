import { beforeEach, describe, expect, it } from 'vitest';

import {
  archiveRow,
  canRestoreAs,
  restoreProblemText,
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
  ])(
    'states what happens next to %s',
    (_name, fields, days, key, purgeable) => {
      const row = archiveRow(entry(fields), { retentionDays: days, ...NAMES });
      expect(row.status).toBe(key ? t(key) : '');
      expect(row.purgeable).toBe(purgeable);
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
});
