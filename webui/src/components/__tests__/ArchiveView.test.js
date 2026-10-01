// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { archiveRetention } from '../../lib/archiveRetention.svelte.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const listMock = vi.fn();
const showMock = vi.fn();
const restoreMock = vi.fn();
const purgeMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('svelte/reactivity', async () => {
  return import('../../../node_modules/svelte/src/reactivity/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listArchiveEntries: (...args) => listMock(...args),
  showArchiveEntry: (...args) => showMock(...args),
  restoreArchiveEntry: (...args) => restoreMock(...args),
  purgeArchiveEntries: (...args) => purgeMock(...args),
}));

const { default: ArchiveView } = await import('../ArchiveView.svelte');

const NO_FILTERS = { kind: '', agentId: '', projectId: '' };

function entry(fields) {
  return {
    state: 'archived',
    project_id: null,
    agent_id: null,
    owner_name: null,
    archived_at: '2026-09-20T10:00:00+00:00',
    purge_at: '2026-10-20T10:00:00+00:00',
    session_count: 1,
    restorable: true,
    not_restorable_reason: null,
    may_hold_user_folders: false,
    ...fields,
  };
}

const CODER = entry({
  entry_id: 'e-coder',
  kind: 'agent',
  subject_id: 'coder',
  agent_id: 'coder',
  label: 'Coder',
  session_count: 2,
});
const NOTES = entry({
  entry_id: 'e-notes',
  kind: 'session',
  subject_id: 'notes',
  agent_id: 'main',
  label: 'Notes',
});
const LEGACY = entry({
  entry_id: 'e-legacy',
  kind: 'files',
  subject_id: 'legacy',
  label: 'legacy',
  purge_at: null,
  session_count: 0,
  restorable: false,
  not_restorable_reason: 'kind_not_restorable',
  may_hold_user_folders: true,
});

// The App navigator goes up with Back when the list is the previous entry,
// and Back only closes an open dialog: going up while one is open stays.
function dialogAwareNavigation() {
  const base = createStandaloneNavigation();
  return {
    active: true,
    get place() {
      return base.place;
    },
    navigate: base.navigate,
    replace: base.replace,
    up: (...args) =>
      document.querySelector('[role="dialog"]') ? false : base.up(...args),
  };
}

function page(entries, { next_cursor = null, retention_days = 30 } = {}) {
  return { entries, next_cursor, retention_days };
}

function detailOf(item, restore = {}) {
  return {
    entry: item,
    sessions: [
      {
        session_id: 'chat',
        project_id: null,
        agent_id: item.subject_id,
        title: 'Release chat',
        created_at: '2026-09-01T10:00:00+00:00',
        last_activity_at: '2026-09-19T10:00:00+00:00',
      },
    ],
    session_count: item.session_count,
    files: {
      state: 'present',
      trees: [
        {
          role: 'agent',
          path: `archive/entries/${item.entry_id}/agent`,
          source_path: `agents/${item.subject_id}`,
          user_folder: false,
        },
      ],
    },
    details: {},
    restore: {
      possible: true,
      target_id: item.subject_id,
      blockers: [],
      warnings: [],
      ...restore,
    },
  };
}

async function waitForCondition(check, attempts = 50) {
  for (let index = 0; index < attempts; index += 1) {
    if (check()) return;
    await Promise.resolve();
    flushSync();
  }
  throw new Error('Condition was not met in time');
}

function rowIds() {
  return [...document.querySelectorAll('.archive-row')].map(
    (row) => row.dataset.entryId,
  );
}

function row(entryId) {
  return document.querySelector(`.archive-row[data-entry-id="${entryId}"]`);
}

function button(label, root = document) {
  const match = [...root.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  expect(match, `button not found: ${label}`).toBeTruthy();
  return match;
}

function dialogButton(label) {
  const footer = document.querySelector('.modal-footer');
  expect(footer, 'dialog not open').toBeTruthy();
  return button(label, footer);
}

function chooseFilter(ariaLabel, optionLabel) {
  document.querySelector(`button[aria-label="${ariaLabel}"]`).click();
  flushSync();
  const option = [...document.querySelectorAll('[role="option"]')].find(
    (item) =>
      item
        .querySelector('.dropdown-primitive__option-label')
        ?.textContent.trim() === optionLabel,
  );
  expect(option, `option not found: ${optionLabel}`).toBeTruthy();
  option.click();
  flushSync();
}

function select(name) {
  document
    .querySelector(
      `[role="checkbox"][aria-label="${t('archive.row.select', { name })}"]`,
    )
    .click();
  flushSync();
}

describe('ArchiveView', () => {
  let mounted;

  // `props` may be a reactive bag a test changes after mounting.
  function mountView(props = {}) {
    props.agents ??= [{ id: 'main', name: 'Main' }];
    props.projects ??= [];
    mounted = mount(ArchiveView, { target: document.body, props });
    flushSync();
  }

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    archiveRetention.days = undefined;
    mounted = null;
    for (const mock of [listMock, showMock, restoreMock, purgeMock]) {
      mock.mockReset();
    }
  });

  afterEach(async () => {
    if (mounted) await unmount(mounted);
    mounted = null;
    document.body.innerHTML = '';
  });

  it('lists entries with when they are deleted, filters them by kind and scope, and pages', async () => {
    const cursor = { archived_at: CODER.archived_at, entry_id: 'e-legacy' };
    listMock
      .mockResolvedValueOnce(
        page([CODER, NOTES, LEGACY], { next_cursor: cursor }),
      )
      .mockResolvedValueOnce(page([entry({ ...NOTES, entry_id: 'e-old' })]))
      .mockResolvedValue(page([]));
    mountView();

    await waitForCondition(() => rowIds().length === 3);
    expect(listMock).toHaveBeenCalledWith({ ...NO_FILTERS, limit: 50 });
    // The period the list carries is the app-wide one the delete dialogs use.
    expect(archiveRetention.days).toBe(30);
    expect(document.body.textContent).toContain(
      t('archive.retention.days', { days: 30 }),
    );
    expect(row('e-coder').textContent).toContain(
      t('archive.row.sessions', { count: 2 }),
    );
    expect(row('e-coder').textContent).toContain('Deleted automatically on');
    expect(row('e-notes').textContent).toContain('Main');
    // Older files are never deleted automatically but can be deleted by hand.
    expect(row('e-legacy').textContent).toContain(
      t('archive.row.neverDeleted'),
    );
    expect(row('e-legacy').textContent).toContain(t('archive.row.deleteOnly'));
    expect(row('e-legacy').querySelector('[role="checkbox"]').disabled).toBe(
      false,
    );

    button(t('archive.loadMore')).click();
    await waitForCondition(() => rowIds().length === 4);
    expect(listMock).toHaveBeenLastCalledWith({
      ...NO_FILTERS,
      cursor,
      limit: 50,
    });
    expect(
      [...document.querySelectorAll('button')].some(
        (item) => item.textContent.trim() === t('archive.loadMore'),
      ),
    ).toBe(false);

    chooseFilter(t('archive.filter.kind'), t('archive.filter.sessions'));
    chooseFilter(t('archive.filter.scope'), 'Main');
    await waitForCondition(() =>
      document.body.textContent.includes(t('archive.emptyFiltered')),
    );
    expect(listMock).toHaveBeenLastCalledWith({
      kind: 'session',
      agentId: 'main',
      projectId: '',
      limit: 50,
    });
  });

  it('opens Restore as when a restore meets a taken ID and restores under the new ID', async () => {
    const navigation = dialogAwareNavigation();
    listMock.mockResolvedValue(page([CODER]));
    showMock.mockResolvedValue(detailOf(CODER));
    restoreMock
      .mockRejectedValueOnce(
        Object.assign(new Error('an Agent with id coder exists'), {
          code: 'archive_restore_conflict',
          details: {
            data: {
              entry_id: 'e-coder',
              kind: 'agent',
              conflicts: [
                {
                  code: 'agent_id_taken',
                  id: 'coder',
                  message: 'an Agent with id coder exists',
                  agent_id: 'coder',
                },
              ],
              fix: 'target_id',
            },
          },
        }),
      )
      .mockResolvedValueOnce({
        entry_id: 'e-coder',
        kind: 'agent',
        subject_id: 'coder',
        restored: { agent_id: 'coder-2' },
        session_count: 2,
        grant_agent_ids: [],
        warnings: [
          {
            code: 'grant_target_missing',
            message: 'Agent manager no longer exists',
            agent_id: 'manager',
          },
        ],
      });
    const onToast = vi.fn();
    mountView({ navigation, onToast });
    await waitForCondition(() => rowIds().length === 1);

    row('e-coder').querySelector('.archive-row__main').click();
    await waitForCondition(() =>
      document.body.textContent.includes('Release chat'),
    );
    expect(navigation.place).toEqual(['e-coder']);
    expect(showMock).toHaveBeenCalledWith('e-coder');

    button(t('archive.action.restore')).click();
    await waitForCondition(() =>
      document.getElementById('archive-restore-as-id'),
    );
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.textContent).toContain(t('archive.restoreAs.title'));
    expect(dialog.textContent).toContain(
      t('archive.restore.conflict', { id: 'coder' }),
    );

    const input = document.getElementById('archive-restore-as-id');
    input.value = 'coder-2';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    dialogButton(t('archive.action.restore')).click();

    await waitForCondition(() => onToast.mock.calls.length === 1);
    expect(restoreMock).toHaveBeenLastCalledWith('e-coder', {
      targetId: 'coder-2',
    });
    expect(onToast).toHaveBeenCalledWith({
      title: t('archive.restore.success', { name: 'Coder' }),
      message: t('archive.problem.grantTargetMissing', { agent: 'manager' }),
      variant: 'warn',
    });
    await waitForCondition(() => navigation.place.length === 0);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
  });

  it('deletes a selection or every matching entry only after a confirmation', async () => {
    listMock.mockResolvedValue(page([CODER, NOTES, LEGACY]));
    purgeMock
      .mockResolvedValueOnce({
        purged: [{ entry_id: 'e-coder' }, { entry_id: 'e-notes' }],
        pending: [],
      })
      .mockResolvedValueOnce({
        purged: [{ entry_id: 'e-coder' }, { entry_id: 'e-notes' }],
        pending: [],
        kept: [{ entry_id: 'e-legacy', reason: 'files' }],
      });
    const onToast = vi.fn();
    mountView({ onToast });
    await waitForCondition(() => rowIds().length === 3);

    select('Coder');
    select('Notes');
    expect(document.body.textContent).toContain(
      t('archive.selection.count', { count: 2 }),
    );
    button(t('archive.deletePermanently')).click();
    flushSync();
    expect(document.querySelector('[role="dialog"]').textContent).toContain(
      t('archive.purgeMany.confirm', { count: 2 }),
    );
    dialogButton(t('common.cancel')).click();
    flushSync();
    expect(purgeMock).not.toHaveBeenCalled();

    button(t('archive.deletePermanently')).click();
    flushSync();
    dialogButton(t('archive.deletePermanently')).click();
    await waitForCondition(() => onToast.mock.calls.length === 1);
    expect(purgeMock).toHaveBeenCalledWith({
      entryIds: ['e-coder', 'e-notes'],
    });
    expect(onToast).toHaveBeenCalledWith({
      title: t('archive.purge.successMany', { count: 2 }),
      variant: 'success',
    });

    // Every matching entry: the files entry may hold the user's own folders,
    // so deleting everything keeps it.
    await waitForCondition(() =>
      document.body.textContent.includes(t('archive.purgeAll.everything')),
    );
    button(t('archive.purgeAll.everything')).click();
    await waitForCondition(() => document.querySelector('[role="dialog"]'));
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.textContent).toContain(t('archive.purgeAll.everythingTitle'));
    expect(dialog.textContent).toContain(
      `${t('archive.purgeMany.confirm', { count: 2 })} ${t('archive.purgeAll.keptOne')}`,
    );
    dialogButton(t('archive.deletePermanently')).click();
    await waitForCondition(() => onToast.mock.calls.length === 2);
    expect(purgeMock).toHaveBeenLastCalledWith({ all: true, ...NO_FILTERS });
    expect(onToast).toHaveBeenLastCalledWith({
      title: t('archive.purge.successMany', { count: 2 }),
      message: t('archive.purge.keptOne'),
      variant: 'success',
    });
  });

  it('reloads on archive changes and says when the shown entry is gone', async () => {
    const navigation = createStandaloneNavigation(['e-gone']);
    const props = reactiveProps({ navigation, archiveRefreshToken: 0 });
    listMock.mockResolvedValue(page([CODER]));
    showMock.mockRejectedValue(
      Object.assign(new Error('unknown archive entry'), {
        code: 'archive_entry_not_found',
      }),
    );
    mountView(props);

    await waitForCondition(() =>
      document.body.textContent.includes(t('archive.detail.notFound')),
    );
    button(t('archive.detail.backToList')).click();
    await waitForCondition(() => rowIds().length === 1);
    const loads = listMock.mock.calls.length;

    props.archiveRefreshToken += 1;
    flushSync();
    await waitForCondition(() => listMock.mock.calls.length === loads + 1);
    expect(listMock).toHaveBeenLastCalledWith({ ...NO_FILTERS, limit: 50 });
  });
});
