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
  pickFolder,
  choose,
  render,
  openSwarm,
  fixtureState,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';

const NEW_RUN = t('swarm.newRun');
const NEW_SWARM = t('swarm.newProfile');
const START_RUN = t('swarm.startButton');
const DELETE_RUN = t('swarm.deleteRun.title');
const RESUME = t('swarm.resume');
const STOP = t('swarm.stop');
const ACTIVE_RUNS = t('swarm.runs.active');
const INACTIVE_RUNS = t('swarm.runs.inactive');

const runGroup = (label) =>
  document.querySelector(`nav[aria-label="${label}"]`);
const dialog = () => document.querySelector('[role="dialog"]');
const confirmDelete = () =>
  [...dialog().querySelectorAll('button')].find(
    (node) => node.textContent.trim() === t('common.delete'),
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
    expect(button(t('common.refresh')).disabled).toBe(false);
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
      [ACTIVE_RUNS, t('swarm.runs.noActive')],
      [INACTIVE_RUNS, t('swarm.runs.noInactive')],
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
      `nav[aria-label="${t('swarm.profiles')}"] button`,
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

  const changed = (resource, id) => ({ resource, ids: [id], revision: 2 });
  // Reads by kind; a Board read after a post number fetches only newer posts.
  const reads = {
    'profiles.list': ([name]) => name === 'profiles.list',
    'swarms.list': ([name]) => name === 'swarms.list',
    'swarms.get': ([name]) => name === 'swarms.get',
    'board.list': ([name]) => name === 'board.list',
    'board.read': ([name, args]) => name === 'board.read' && !('after' in args),
    'board.read after': ([name, args]) =>
      name === 'board.read' && 'after' in args,
  };
  const none = Object.fromEntries(Object.keys(reads).map((name) => [name, 0]));
  const everything = {
    ...none,
    'profiles.list': 1,
    'swarms.list': 1,
    'swarms.get': 1,
    'board.list': 1,
    'board.read': 1,
  };
  const open = { ...none, 'swarms.get': 1 };
  it.each([
    [
      "the open Swarm's list entry",
      [changed('swarms', 'swr-a')],
      { ...open, 'swarms.list': 1 },
    ],
    ["the open Swarm's participants", [changed('participants', 'swr-a')], open],
    [
      "the open Swarm's posts",
      [changed('posts', 'swr-a')],
      { ...open, 'board.read after': 1 },
    ],
    [
      "the open Swarm's discussions",
      [changed('discussions', 'swr-a')],
      { ...open, 'board.list': 1 },
    ],
    ["the open Swarm's hidden Wiki", [changed('wiki', 'swr-a')], open],
    [
      "another Swarm's list entry",
      [changed('swarms', 'swr-b')],
      { ...none, 'swarms.list': 1 },
    ],
    ["another Swarm's posts", [changed('posts', 'swr-b')], none],
    [
      'a profile',
      [changed('profiles', 'prf-a')],
      { ...none, 'profiles.list': 1 },
    ],
    [
      'a burst of another Swarm and a profile',
      [changed('swarms', 'swr-b'), changed('profiles', 'prf-a')],
      { ...none, 'swarms.list': 1, 'profiles.list': 1 },
    ],
    ['an unknown resource', [changed('logs', 'swr-a')], everything],
    ['unnamed records', [null], everything],
  ])(
    'reloads only what shows a change of %s',
    async (_name, changes, expected) => {
      const { bridge, operation } = createBridge();
      await openSwarm(bridge);
      await settle(150);
      const counts = () =>
        Object.fromEntries(
          Object.entries(reads).map(([name, matches]) => [
            name,
            operation.mock.calls.filter(matches).length,
          ]),
        );
      const before = counts();
      for (const change of changes) bridge.invalidate(change);
      await settle(150);
      await vi.waitFor(() =>
        expect(
          Object.fromEntries(
            Object.entries(counts()).map(([name, count]) => [
              name,
              count - before[name],
            ]),
          ),
        ).toEqual(expected),
      );
    },
  );

  it('describes a Swarm by participants per Model and a Run by its Swarm and size', async () => {
    const formation = {
      ...profile,
      participants: [
        { model: 'demo/model', count: 2 },
        { model: 'demo/other', count: 1 },
        { model: 'demo/model', count: 1 },
      ],
    };
    const { bridge, operation } = createBridge(formation);
    overrideOperations(operation, {
      'swarms.list': () => ({
        entries: [
          {
            id: 'swr-a',
            title: 'Investigate',
            name: 'Research',
            participant_count: 4,
            state: 'running',
          },
        ],
      }),
    });
    await render(bridge);
    const tooltip = () => document.querySelector('#app-tooltip');
    const rows = () =>
      [...tooltip().querySelectorAll('dt')].map((label) => [
        label.textContent,
        label.nextElementSibling.textContent,
      ]);
    button('Research (4)').focus();
    await vi.waitFor(() =>
      expect(tooltip()?.textContent).toContain('demo/other'),
    );
    expect(tooltip().querySelector('.app-tooltip__title').textContent).toBe(
      'Research',
    );
    expect(rows()).toEqual([
      [t('swarm.participantCount', { count: 3 }), 'demo/model'],
      [t('swarm.participantCount.one'), 'demo/other'],
    ]);
    button('Investigate').focus();
    await vi.waitFor(() =>
      expect(tooltip().querySelector('.app-tooltip__title').textContent).toBe(
        'Investigate',
      ),
    );
    expect(rows()).toEqual([
      [t('swarm.profile'), 'Research'],
      [t('swarm.participants'), '4'],
    ]);
  });

  it('marks New run as the current entry while the goal form is shown', async () => {
    const { bridge } = createBridge();
    await render(bridge);
    expect(button(NEW_RUN).getAttribute('aria-current')).toBe('page');
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
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

  it('shows the route the host sends and pushes Swarm and tab choices as steps', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/swarms/swr-a');
    button(t('swarm.tabs.usage')).click();
    await vi.waitFor(() =>
      expect(bridge.pushRoute).toHaveBeenLastCalledWith('/swarms/swr-a/usage'),
    );
    // Back to the empty route shows the start page, Forward the tab again.
    bridge.goTo('');
    await tick();
    expect(document.querySelector('.swarm-head')).toBeNull();
    expect(button(NEW_RUN).getAttribute('aria-current')).toBe('page');
    bridge.goTo('/swarms/swr-a/usage');
    await vi.waitFor(() =>
      expect(document.querySelector('.usage-summary')).not.toBeNull(),
    );
    // A Swarm that is gone leaves the shown one and corrects the route.
    overrideOperations(operation, {
      'swarms.get': (args, fallback) =>
        args.swarm_id === 'swr-gone'
          ? Promise.reject(new Error('test-owned missing Swarm'))
          : fallback(),
    });
    bridge.goTo('/swarms/swr-gone');
    await vi.waitFor(() =>
      expect(bridge.replaceRoute).toHaveBeenLastCalledWith(
        '/swarms/swr-a/usage',
      ),
    );
    expect(document.querySelector('.usage-summary')).not.toBeNull();
    expect(bridge.pushRoute).toHaveBeenCalledTimes(2);
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
    expect(bridge.route).toBe('/swarms/swr-b');
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
    // The field completes the server's folders through the page bridge.
    await pickFolder('swarm-start-directory', 'D:/r', 'run-only');
    expect(bridge.listDirectory).toHaveBeenCalledWith({
      path: 'D:/',
      include_files: false,
      prefix: 'r',
    });
    fill('swarm-goal', 'goal-sentinel');
    await tick();
    bridge.invalidate();
    await settle();
    expect(input.value).toBe('D:/run-only/');
    button(START_RUN).click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith(
        'swarms.start',
        expect.objectContaining({
          profile_id: profile.id,
          prompt: 'goal-sentinel',
          working_directory: 'D:/run-only/',
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
    // The completed default keeps its separator, yet stays the Project's.
    await pickFolder('swarm-start-directory', 'C:/p', 'project');
    expect(directory().value).toBe('C:/project/');
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
  it.each([
    ['cancelled', []],
    // An open Run whose participants all idle is stopped before deletion.
    ['idle', ['swarms.stop']],
  ])(
    'confirms deletion of a %s Run, keeps the profile and clears the selected Swarm',
    async (state, before) => {
      const { bridge, operation } = createBridge(profile, {
        ...structuredClone(swarm),
        state,
        participants: swarm.participants.map((participant) => ({
          ...structuredClone(participant),
          state: 'idle',
        })),
      });
      let deleted = false;
      overrideOperations(operation, {
        'swarms.stop': () => ({ swarm_id: 'swr-a', state: 'stopped' }),
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
      expect(dialog().textContent).toContain(t('swarm.deleteRun.body'));
      // Back in the app dismisses the confirmation like Cancel.
      bridge.back();
      await tick();
      expect(dialog()).toBeNull();
      expect(callsTo(operation, 'swarms.delete')).toHaveLength(0);
      button(DELETE_RUN).click();
      await tick();
      confirmDelete().click();
      await settle();
      expect(operation).toHaveBeenCalledWith('swarms.delete', {
        swarm_id: 'swr-a',
      });
      expect(
        operation.mock.calls
          .map(([name]) => name)
          .filter((name) => ['swarms.stop', 'swarms.delete'].includes(name)),
      ).toEqual([...before, 'swarms.delete']);
      expect(callsTo(operation, 'profiles.delete')).toHaveLength(0);
      expect(dialog()).toBeNull();
      expect(button('Investigate')).toBeUndefined();
      expect(bridge.replaceRoute).toHaveBeenLastCalledWith('');
      expect(button('Research')).toBeDefined();
    },
  );

  it('retains the Run deletion confirmation and allows retry after a failure', async () => {
    const { bridge, operation } = createBridge(profile, {
      ...structuredClone(swarm),
      state: 'deleting',
      participants: swarm.participants.map((participant) => ({
        ...structuredClone(participant),
        state: 'cancelled',
      })),
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
    ['needs_attention', ['running', 'failed'], 'stop'],
    ['idle', ['idle', { state: 'idle', run_active: true }], 'stop'],
    ['preparing', ['idle', 'idle'], 'stop'],
    ['idle', ['idle', 'idle'], 'resume'],
    ['needs_attention', ['idle', 'failed'], 'resume'],
    ['cancelled', ['cancelled', 'cancelled'], 'resume'],
    ['interrupted', ['interrupted', 'idle'], 'resume'],
  ])(
    'offers one lifecycle action for a %s Swarm with %j participants: %s',
    async (swarmState, states, action) => {
      const { bridge, operation } = createBridge(profile, {
        ...structuredClone(swarm),
        state: swarmState,
        participants: swarm.participants.map((participant, index) => ({
          ...structuredClone(participant),
          ...(typeof states[index] === 'string'
            ? { state: states[index] }
            : states[index]),
        })),
      });
      await openSwarm(bridge);
      await settle();
      const [shown, hidden] =
        action === 'stop' ? [STOP, RESUME] : [RESUME, STOP];
      expect(button(hidden)).toBeUndefined();
      // Deletion waits until nothing works any more.
      expect(button(DELETE_RUN).disabled).toBe(action === 'stop');
      button(shown).click();
      await tick();
      expect(operation).toHaveBeenCalledWith(
        `swarms.${action}`,
        expect.objectContaining({ swarm_id: 'swr-a' }),
      );
    },
  );

  it('names participants whose Resume failed although the operation succeeded', async () => {
    const { bridge, operation } = createBridge(profile, {
      ...structuredClone(swarm),
      state: 'idle',
      participants: swarm.participants.map((participant) => ({
        ...structuredClone(participant),
        state: 'idle',
      })),
    });
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
    button(t('swarm.communication.title')).click();
    flushSync();
    const select = document.querySelector('.communication select');
    select.value = 'pull';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await tick();
    expect(callsTo(operation, 'swarms.settings')).toHaveLength(0);
    expect(document.body.textContent).toContain(
      t('swarm.communication.proposed'),
    );
    button(t('swarm.apply')).click();
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
