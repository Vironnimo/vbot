// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  tick,
  SwarmPage,
  profile,
  swarm,
  button,
  createBridge,
  overrideOperations,
  callsTo,
  settle,
  fill,
  choose,
  render,
  openSwarm,
  fixtureState,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';

const NEW_RUN = t('swarm.newRun', 'New run');
const NEW_SWARM = t('swarm.newProfile', 'New Swarm');
const START_RUN = t('swarm.startButton', 'Start Run');
const DELETE_RUN = t('swarm.deleteRun.title', 'Delete Run');
const RESUME = t('swarm.resume', 'Resume');
const STOP = t('swarm.stop', 'Stop');
const ACTIVE_RUNS = t('swarm.runs.active', 'Active runs');
const INACTIVE_RUNS = t('swarm.runs.inactive', 'Inactive runs');

const runGroup = (label) =>
  document.querySelector(`nav[aria-label="${label}"]`);
const dialog = () => document.querySelector('[role="dialog"]');
const confirmDelete = () =>
  [...dialog().querySelectorAll('button')].find(
    (node) => node.textContent.trim() === t('common.delete', 'Delete'),
  );

describe('SwarmPage overview', () => {
  it('shows a connection failure until the host initializes the page', async () => {
    vi.useFakeTimers();
    const { bridge } = createBridge();
    fixtureState.mounted = mount(SwarmPage, {
      target: document.body,
      props: { bridgeClient: bridge },
    });
    flushSync();
    await vi.advanceTimersByTimeAsync(10_000);
    flushSync();
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    expect(button(t('common.refresh', 'Refresh')).disabled).toBe(false);
    bridge.show();
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(button(NEW_SWARM)).toBeDefined();
  });

  it('loads retained lists without requesting the profile catalog', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    expect(button(NEW_SWARM)).toBeDefined();
    expect(runGroup(INACTIVE_RUNS)).not.toBeNull();
    expect(operation).toHaveBeenCalledWith('profiles.list', { limit: 100 });
    expect(callsTo(operation, 'catalog')).toHaveLength(0);
    bridge.show();
    await tick();
    expect(callsTo(operation, 'profiles.list')).toHaveLength(1);
    button(NEW_SWARM).click();
    await tick();
    flushSync();
    expect(callsTo(operation, 'catalog')).toHaveLength(1);
    expect(document.querySelector('input')).not.toBeNull();
  });

  it('groups Runs by execution state under headed groups that explain when empty', async () => {
    const { bridge, operation } = createBridge();
    let entries = [];
    overrideOperations(operation, { 'swarms.list': () => ({ entries }) });
    await render(bridge);
    const emptyText = (label) =>
      runGroup(label).querySelector('p')?.textContent.trim();
    const rows = (label) => runGroup(label).querySelectorAll('button');
    for (const [label, empty] of [
      [ACTIVE_RUNS, t('swarm.runs.noActive', 'No active runs')],
      [INACTIVE_RUNS, t('swarm.runs.noInactive', 'No inactive runs')],
    ]) {
      const section = runGroup(label).closest('section');
      expect(
        document.getElementById(section.getAttribute('aria-labelledby'))
          .tagName,
      ).toBe('H3');
      expect(rows(label)).toHaveLength(0);
      expect(emptyText(label)).toBe(empty);
    }
    entries = [
      'preparing',
      'running',
      'stopping',
      'idle',
      'needs_attention',
      'interrupted',
      'stopped',
      'failed',
    ].map((state) => ({ id: state, title: `goal-${state}`, state }));
    bridge.invalidate();
    await vi.waitFor(() => expect(rows(ACTIVE_RUNS)).toHaveLength(3));
    expect([...rows(ACTIVE_RUNS)].map((row) => row.textContent.trim())).toEqual(
      ['goal-preparing', 'goal-running', 'goal-stopping'],
    );
    expect(rows(INACTIVE_RUNS)).toHaveLength(5);
    expect(emptyText(ACTIVE_RUNS)).toBeUndefined();
    expect(emptyText(INACTIVE_RUNS)).toBeUndefined();
    const profileRow = document.querySelector(
      `nav[aria-label="${t('swarm.profiles', 'Swarms')}"] button`,
    );
    expect(profileRow.textContent.trim()).toBe('Research (2)');
    expect(profileRow.children).toHaveLength(1);
    expect(document.querySelector('.run-group strong')).toBeNull();
    entries = entries.map((entry) =>
      entry.id === 'running' ? { ...entry, state: 'idle' } : entry,
    );
    bridge.invalidate();
    await vi.waitFor(() => expect(rows(ACTIVE_RUNS)).toHaveLength(2));
    expect(runGroup(INACTIVE_RUNS).textContent).toContain('goal-running');
  });

  it('marks New run as the current entry while the goal form is shown', async () => {
    const { bridge } = createBridge();
    await render(bridge);
    expect(button(NEW_RUN).getAttribute('aria-current')).toBe('page');
    await openSwarm(bridge);
    expect(button(NEW_RUN).getAttribute('aria-current')).toBeNull();
    button(NEW_RUN).click();
    await tick();
    expect(document.querySelector('.swarm-head')).toBeNull();
    expect(button(NEW_RUN).getAttribute('aria-current')).toBe('page');
    button('Research').click();
    await tick();
    flushSync();
    expect(button(NEW_RUN).getAttribute('aria-current')).toBeNull();
  });

  it('keeps the overview usable when the profile catalog fails and retries on request', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    operation.mockRejectedValueOnce(new Error('catalog-unavailable-test'));
    button(NEW_SWARM).click();
    await tick();
    flushSync();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      'catalog-unavailable-test',
    );
    expect(button(NEW_SWARM).disabled).toBe(false);
    button(NEW_SWARM).click();
    await tick();
    flushSync();
    expect(document.querySelector('input')).not.toBeNull();
  });

  it('ignores old Swarm detail replies after selecting a new Swarm', async () => {
    const { bridge, operation } = createBridge();
    const other = {
      ...structuredClone(swarm),
      id: 'swr-b',
      prompt: 'Second swarm',
      main_discussion_id: 'dsc-b',
    };
    let finishDiscussions;
    overrideOperations(operation, {
      'swarms.list': () => ({ entries: [swarm, other] }),
      'swarms.get': (args) => ({
        swarm: structuredClone(args.swarm_id === 'swr-b' ? other : swarm),
      }),
      'board.list': (args) =>
        args.swarm_id === 'swr-a'
          ? new Promise((resolve) => {
              finishDiscussions = resolve;
            })
          : { entries: [{ id: 'dsc-b', title: 'Second discussion' }] },
      'board.read': (args) => ({
        entries: [
          { id: args.swarm_id, text: args.swarm_id, sender_id: 'prt-a' },
        ],
      }),
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(finishDiscussions).toBeTypeOf('function'));
    button('Second swarm').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.board').textContent).toContain('swr-b'),
    );
    finishDiscussions({
      entries: [{ id: 'dsc-main', title: 'Old discussion' }],
    });
    await settle();
    expect(document.querySelector('.board').textContent).toContain('swr-b');
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/swarms/swr-b');
    expect(document.getElementById('swarm-discussion').value).toBe('dsc-b');
  });
});

describe('Swarm Run start', () => {
  const projectProfile = {
    ...profile,
    working_directory: { kind: 'project', project_id: 'project-a' },
  };
  const directoryProfile = { ...profile, id: 'prf-b', name: 'Other' };
  const directory = () => document.getElementById('swarm-start-directory');

  it('prefills the Run directory, preserves edits during invalidation and submits only the override', async () => {
    const { bridge, operation } = createBridge();
    overrideOperations(operation, {
      'swarms.start': () => ({ swarm_id: swarm.id }),
    });
    await render(bridge);
    const input = directory();
    expect(input.value).toBe('C:/work');
    const form = document.querySelector('.start');
    expect(
      [...form.querySelectorAll('[id]')]
        .map((el) => el.id)
        .filter((id) =>
          [
            'swarm-start-profile',
            'swarm-start-directory',
            'swarm-goal',
          ].includes(id),
        ),
    ).toEqual(['swarm-start-profile', 'swarm-start-directory', 'swarm-goal']);
    fill('swarm-start-directory', 'D:/run-only');
    fill('swarm-goal', 'goal-sentinel');
    await tick();
    bridge.invalidate();
    await settle();
    expect(input.value).toBe('D:/run-only');
    button(START_RUN).click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith(
        'swarms.start',
        expect.objectContaining({
          profile_id: profile.id,
          prompt: 'goal-sentinel',
          working_directory: 'D:/run-only',
        }),
      ),
    );
    expect(callsTo(operation, 'profiles.save')).toHaveLength(0);
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    button(NEW_RUN).click();
    await tick();
    expect(directory().value).toBe('C:/work');
  });

  it('resolves Project defaults, preserves their Project selection and replaces the default on Swarm selection', async () => {
    const { bridge, operation } = createBridge(projectProfile);
    overrideOperations(operation, {
      'profiles.list': () => ({ entries: [projectProfile, directoryProfile] }),
      'swarms.start': () => ({ swarm_id: swarm.id }),
    });
    await render(bridge);
    await vi.waitFor(() => expect(directory().value).toBe('C:/project'));
    fill('swarm-goal', 'project-goal');
    await tick();
    button(START_RUN).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    expect(callsTo(operation, 'swarms.start')[0][1]).not.toHaveProperty(
      'working_directory',
    );
    button(NEW_RUN).click();
    await tick();
    await choose('swarm-start-profile', 'Other');
    expect(directory().value).toBe('C:/work');
  });

  it('ignores stale Project lookups and prevents starting with a blank directory', async () => {
    const { bridge, operation } = createBridge(projectProfile);
    let resolveCatalog;
    overrideOperations(operation, {
      'profiles.list': () => ({ entries: [projectProfile, directoryProfile] }),
      catalog: () =>
        new Promise((resolve) => {
          resolveCatalog = resolve;
        }),
    });
    await render(bridge);
    expect(button(START_RUN).disabled).toBe(true);
    await choose('swarm-start-profile', 'Other');
    resolveCatalog({
      catalog: { projects: [{ id: 'project-a', cwd: 'C:/late' }] },
    });
    await tick();
    expect(directory().value).toBe('C:/work');
    fill('swarm-start-directory', '  ');
    await tick();
    expect(button(START_RUN).disabled).toBe(true);
  });
});

describe('Swarm Run controls', () => {
  it('requires Stop before a Swarm can be deleted', async () => {
    const { bridge, operation } = createBridge();
    await openSwarm(bridge);
    await settle();
    expect(button(DELETE_RUN).disabled).toBe(true);
    button(DELETE_RUN).click();
    expect(callsTo(operation, 'swarms.delete')).toHaveLength(0);
  });

  it('confirms deletion, keeps the profile and clears the selected Swarm', async () => {
    const { bridge, operation } = createBridge(profile, {
      ...structuredClone(swarm),
      state: 'cancelled',
    });
    let deleted = false;
    overrideOperations(operation, {
      'swarms.delete': () => {
        deleted = true;
        return { deleted: true };
      },
      'swarms.list': (_args, fallback) =>
        deleted ? { entries: [], has_more: false } : fallback(),
    });
    await openSwarm(bridge);
    await settle();
    button(DELETE_RUN).click();
    await tick();
    expect(dialog().textContent).toContain(
      t(
        'swarm.deleteRun.body',
        'Permanently delete this Run, its Board, Wiki and participant Sessions? The Swarm will be kept. This cannot be undone.',
      ),
    );
    button(t('common.cancel', 'Cancel')).click();
    await tick();
    expect(callsTo(operation, 'swarms.delete')).toHaveLength(0);
    button(DELETE_RUN).click();
    await tick();
    confirmDelete().click();
    await settle();
    expect(operation).toHaveBeenCalledWith('swarms.delete', {
      swarm_id: 'swr-a',
    });
    expect(callsTo(operation, 'profiles.delete')).toHaveLength(0);
    expect(dialog()).toBeNull();
    expect(button('Investigate')).toBeUndefined();
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
    expect(button('Research')).toBeDefined();
  });

  it('retains the Run deletion confirmation and allows retry after a failure', async () => {
    const { bridge, operation } = createBridge(profile, {
      ...structuredClone(swarm),
      state: 'deleting',
    });
    overrideOperations(operation, {
      'swarms.delete': () => Promise.reject(new Error('Storage unavailable')),
    });
    await openSwarm(bridge);
    await settle();
    expect(button(RESUME)).toBeUndefined();
    button(DELETE_RUN).click();
    await tick();
    confirmDelete().click();
    await settle();
    expect(dialog().textContent).toContain('Storage unavailable');
    expect(confirmDelete().disabled).toBe(false);
    confirmDelete().click();
    await settle();
    expect(callsTo(operation, 'swarms.delete')).toHaveLength(2);
  });

  it.each([
    ['running', 'idle', true],
    ['cancelled', 'cancelled', false],
    ['interrupted', 'interrupted', false],
  ])(
    'offers Resume for a %s Swarm with a %s participant (Stop offered: %s)',
    async (swarmState, participantState, stopOffered) => {
      const { bridge, operation } = createBridge(profile, {
        ...structuredClone(swarm),
        state: swarmState,
        participants: [
          {
            ...structuredClone(swarm.participants[0]),
            state: participantState,
          },
        ],
      });
      await openSwarm(bridge);
      await settle();
      expect(button(STOP) !== undefined).toBe(stopOffered);
      button(RESUME).click();
      await tick();
      expect(operation).toHaveBeenCalledWith(
        'swarms.resume',
        expect.objectContaining({ swarm_id: 'swr-a' }),
      );
    },
  );

  it('names participants whose Resume failed although the operation succeeded', async () => {
    const { bridge, operation } = createBridge();
    overrideOperations(operation, {
      'swarms.resume': () => ({
        runs: [{ participant_id: 'prt-b', error: 'RuntimeError' }],
      }),
    });
    await openSwarm(bridge);
    await vi.waitFor(() => expect(button(RESUME)).toBeDefined());
    button(RESUME).click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(
        'Beta',
      ),
    );
  });

  it('keeps failed participants visible while their peers are running', async () => {
    const detail = structuredClone(swarm);
    detail.participants[1].state = 'failed';
    const { bridge } = createBridge(profile, detail);
    await openSwarm(bridge);
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')?.textContent).toContain(
        'Beta',
      ),
    );
  });

  it('does not autosave delivery changes and stops only on an explicit click', async () => {
    const { bridge, operation } = createBridge();
    await openSwarm(bridge);
    await tick();
    flushSync();
    button(
      t('swarm.communication.title', 'Change communication settings'),
    ).click();
    flushSync();
    const select = document.querySelector('.communication select');
    select.value = 'pull';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(callsTo(operation, 'swarms.settings')).toHaveLength(0);
    expect(document.body.textContent).toContain(
      t('swarm.communication.proposed', 'Proposed changes'),
    );
    button(t('swarm.apply', 'Apply changes')).click();
    await settle();
    expect(operation).toHaveBeenCalledWith(
      'swarms.settings',
      expect.objectContaining({
        swarm_id: 'swr-a',
        expected_revision: 3,
        delivery: expect.objectContaining({
          main: expect.objectContaining({ mode: 'pull' }),
        }),
      }),
    );
    button(STOP).click();
    await tick();
    expect(operation).toHaveBeenCalledWith(
      'swarms.stop',
      expect.objectContaining({
        swarm_id: 'swr-a',
        request_id: expect.any(String),
      }),
    );
  });
});
