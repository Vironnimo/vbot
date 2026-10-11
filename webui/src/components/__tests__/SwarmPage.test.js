// @vitest-environment jsdom
import { describe, expect, it, onTestFinished, vi } from 'vitest';
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
const DELETE = t('swarm.menu.delete');
const RESUME = t('swarm.resume');
const STOP = t('swarm.stop');
const ACTIVE_RUNS = t('swarm.runs.active');
const INACTIVE_RUNS = t('swarm.runs.inactive');

// A Run group of the pane by its heading; the group stays without Runs.
const runGroup = (label) =>
  [...document.querySelectorAll('.swarm-pane section')].find(
    (section) => section.querySelector('h3').textContent.trim() === label,
  );
const runRows = (label) => runGroup(label).querySelectorAll('[data-run-id]');
const dialog = () => document.querySelector('[role="dialog"]');
const confirmDelete = () =>
  [...dialog().querySelectorAll('button')].find(
    (node) => node.textContent.trim() === t('common.delete'),
  );
// A button of the shown Run's header by its text or accessible name.
const headButton = (text) =>
  [...document.querySelectorAll('.swarm-head button')].find((item) =>
    (item.getAttribute('aria-label') || item.textContent).includes(text),
  );
const menuLabels = () =>
  [...document.querySelectorAll('[role="menuitem"] .context-menu__label')].map(
    (label) => label.textContent.trim(),
  );
const menuItem = (label) =>
  [...document.querySelectorAll('[role="menuitem"]')].find(
    (item) =>
      item.querySelector('.context-menu__label').textContent.trim() === label,
  );

// The narrow page's picker: the trigger shows the chosen entry; the open
// panel lists the entries by group.
const picker = () => document.getElementById('swarm-picker');
const pickerValue = () =>
  picker()
    .querySelector('.searchable-dropdown__trigger-label')
    .textContent.trim();
const pickerPanel = () => document.querySelector('.swarm-bar__panel');
const pickerOption = (label) =>
  [...pickerPanel().querySelectorAll('[role="option"]')].find(
    (option) =>
      option
        .querySelector('.searchable-dropdown__option-label')
        .textContent.trim() === label,
  );

async function pick(label) {
  picker().click();
  await vi.waitFor(() => expect(pickerOption(label)).toBeDefined());
  pickerOption(label).click();
  await tick();
}

// Opens the shown Run's menu in its header.
async function openRunMenu() {
  headButton(t('swarm.run.actions', { title: 'Investigate' })).click();
  await tick();
}

// Chooses `label` from the shown Run's menu.
async function chooseRunAction(label) {
  await openRunMenu();
  menuItem(label).click();
  await tick();
}

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
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      t('swarm.hostUnavailable'),
    );
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
    expect(document.querySelector('.swarm-profile-editor')).not.toBeNull();
  });

  it('groups Runs by execution state under headed groups that explain when empty', async () => {
    const { bridge, operation } = createBridge();
    let entries = [];
    overrideOperations(operation, { 'swarms.list': () => ({ entries }) });
    await render(bridge);
    const emptyText = (label) =>
      runGroup(label)
        .querySelector('.empty-state__description')
        ?.textContent.trim();
    const titles = (label) =>
      [...runRows(label)].map((row) =>
        row.querySelector('.swarm-row__name').textContent.trim(),
      );
    for (const [label, empty] of [
      [ACTIVE_RUNS, t('swarm.runs.noActive')],
      [INACTIVE_RUNS, t('swarm.runs.noInactive')],
    ]) {
      const section = runGroup(label);
      expect(
        document.getElementById(section.getAttribute('aria-labelledby'))
          .tagName,
      ).toBe('H3');
      expect(runRows(label)).toHaveLength(0);
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
    // Open Runs come first, a Run that needs the user among them.
    await vi.waitFor(() => expect(runRows(ACTIVE_RUNS)).toHaveLength(5));
    expect(titles(ACTIVE_RUNS)).toEqual([
      'goal-preparing',
      'goal-running',
      'goal-stopping',
      'goal-idle',
      'goal-needs_attention',
    ]);
    expect(titles(INACTIVE_RUNS)).toEqual([
      'goal-interrupted',
      'goal-stopped',
      'goal-failed',
    ]);
    // A row's second line names its state in words.
    expect(
      runRows(ACTIVE_RUNS)[4]
        .querySelector('.swarm-row__detail')
        .textContent.trim(),
    ).toBe(t('swarm.state.needsAttention'));
    expect(emptyText(ACTIVE_RUNS)).toBeUndefined();
    expect(emptyText(INACTIVE_RUNS)).toBeUndefined();
    const profileRow = document.querySelector(
      `nav[aria-label="${t('swarm.profiles')}"] [data-profile-id]`,
    );
    expect(profileRow.querySelector('.swarm-row__name').textContent).toBe(
      'Research',
    );
    expect(profileRow.querySelector('.swarm-row__detail').textContent).toBe(
      t('swarm.sidebar.profileSummary', {
        size: t('swarm.participantCount', { count: 2 }),
        models: 'model',
      }),
    );
    entries = entries.map((entry) =>
      entry.id === 'running' ? { ...entry, state: 'stopped' } : entry,
    );
    bridge.invalidate();
    await vi.waitFor(() => expect(runRows(ACTIVE_RUNS)).toHaveLength(4));
    expect(titles(INACTIVE_RUNS)).toContain('goal-running');
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

  it('describes a Swarm by participants per Model and a Run by its Swarm, state and size', async () => {
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
    document.querySelector('[data-profile-id="prf-a"]').focus();
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
    document.querySelector('[data-run-id="swr-a"]').focus();
    await vi.waitFor(() =>
      expect(tooltip().querySelector('.app-tooltip__title').textContent).toBe(
        'Investigate',
      ),
    );
    // A Run without a start time leaves out its start.
    expect(rows()).toEqual([
      [t('swarm.profile'), 'Research'],
      [t('swarm.details.state'), t('swarm.state.running')],
      [t('swarm.participants'), t('swarm.participantCount', { count: 4 })],
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

  it("offers the pane's entries as one grouped picker that loads more in its footer", async () => {
    const { bridge, operation } = createBridge();
    overrideOperations(operation, {
      'profiles.list': async ({ cursor }, fallback) =>
        cursor
          ? { entries: [{ ...profile, id: 'prf-b', name: 'Second swarm' }] }
          : { ...(await fallback()), cursor: 'profiles-2' },
      'swarms.list': ({ cursor }) =>
        cursor
          ? { entries: [{ id: 'swr-c', title: 'Archived', state: 'stopped' }] }
          : {
              entries: [
                { id: 'swr-a', title: 'Investigate', state: 'running' },
                { id: 'swr-b', title: 'Survey', state: 'interrupted' },
              ],
              cursor: 'swarms-2',
            },
    });
    await render(bridge);
    expect(pickerValue()).toBe(NEW_RUN);
    expect(picker().getAttribute('aria-label')).toBe(
      t('swarm.sidebar.picker', { current: NEW_RUN }),
    );
    picker().click();
    await vi.waitFor(() => expect(pickerPanel()).not.toBeNull());
    const labels = (selector) =>
      [...pickerPanel().querySelectorAll(selector)].map((node) =>
        node.textContent.trim(),
      );
    expect(labels('.searchable-dropdown__group-label')).toEqual([
      t('swarm.profiles'),
      ACTIVE_RUNS,
      INACTIVE_RUNS,
    ]);
    expect(labels('.searchable-dropdown__option-label')).toEqual([
      NEW_RUN,
      'Research',
      'Investigate',
      'Survey',
    ]);
    // A Run's secondary text is its row's second line.
    expect(
      pickerOption('Survey')
        .querySelector('.searchable-dropdown__option-meta')
        .textContent.trim(),
    ).toBe(t('swarm.state.interrupted'));
    const footer = pickerPanel().querySelector('.searchable-dropdown__footer');
    expect(footer.textContent.trim()).toBe(t('swarm.sidebar.more'));
    footer.click();
    await vi.waitFor(() =>
      expect(labels('.searchable-dropdown__option-label')).toEqual([
        NEW_RUN,
        'Research',
        'Second swarm',
        'Investigate',
        'Survey',
        'Archived',
      ]),
    );
    expect(operation).toHaveBeenCalledWith('profiles.list', {
      limit: 100,
      cursor: 'profiles-2',
    });
    expect(operation).toHaveBeenCalledWith('swarms.list', {
      limit: 100,
      cursor: 'swarms-2',
    });
    expect(
      pickerPanel().querySelector('.searchable-dropdown__footer'),
    ).toBeNull();
  });

  it('opens the chosen picker entry through the leave guards and follows what the page shows', async () => {
    const { bridge } = createBridge();
    await render(bridge);
    await pick('Investigate');
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/swarms/swr-a');
    expect(pickerValue()).toBe('Investigate');
    await pick('Research');
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-profile-editor')).not.toBeNull(),
    );
    expect(bridge.pushRoute).toHaveBeenLastCalledWith('/profiles/prf-a');
    expect(pickerValue()).toBe('Research');
    await pick(NEW_RUN);
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-profile-editor')).toBeNull(),
    );
    expect(pickerValue()).toBe(NEW_RUN);
    // Back and Forward move the value with the shown place.
    bridge.goTo('/swarms/swr-a');
    await vi.waitFor(() => expect(pickerValue()).toBe('Investigate'));
    // A changed new Swarm asks before the picker leaves it.
    document
      .querySelector(`.swarm-bar button[aria-label="${NEW_SWARM}"]`)
      .click();
    await vi.waitFor(() =>
      expect(document.getElementById('swarm-profile-name')).not.toBeNull(),
    );
    expect(pickerValue()).toBe(NEW_SWARM);
    fill('swarm-profile-name', 'Unfinished');
    await tick();
    await pick('Investigate');
    await vi.waitFor(() => expect(dialog()).not.toBeNull());
    [...dialog().querySelectorAll('button')]
      .find(
        (node) => node.textContent.trim() === t('swarm.profile.keepEditing'),
      )
      .click();
    await tick();
    expect(document.getElementById('swarm-profile-name').value).toBe(
      'Unfinished',
    );
    expect(pickerValue()).toBe(NEW_SWARM);
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
    // The failed step leaves the route at the start page it still shows.
    await vi.waitFor(() => expect(bridge.route).toBe(''));
    button(NEW_SWARM).click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-profile-editor')).not.toBeNull(),
    );
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
    expect(document.getElementById('swarm-discussion').textContent).toContain(
      t('swarm.board.mainDiscussion'),
    );
  });

  it('offers the Run and Swarm actions on their pane rows', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    document.querySelector('[data-run-id="swr-a"]').dispatchEvent(
      new MouseEvent('contextmenu', {
        bubbles: true,
        cancelable: true,
        clientX: 20,
        clientY: 20,
      }),
    );
    await tick();
    expect(
      document.querySelector('[role="menu"]').getAttribute('aria-label'),
    ).toBe(t('swarm.run.actions', { title: 'Investigate' }));
    expect(menuLabels()).toEqual([
      t('swarm.run.communication'),
      t('swarm.run.details'),
      DELETE,
    ]);
    // A working Run is stopped before it can be deleted.
    expect(menuItem(DELETE).disabled).toBe(true);
    menuItem(t('swarm.run.details')).click();
    // Another Run's details load first; they name its ID with a copy action.
    await vi.waitFor(() => expect(dialog()?.textContent).toContain('swr-a'));
    expect(callsTo(operation, 'swarms.get')).toHaveLength(1);
    expect(
      dialog().querySelector(`[aria-label="${t('swarm.details.copyId')}"]`),
    ).not.toBeNull();
    bridge.back();
    await tick();
    expect(dialog()).toBeNull();
    // From a shown Run, a Swarm's keyboard menu starts a Run of that Swarm.
    button('Investigate').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.swarm-head')).not.toBeNull(),
    );
    document.querySelector('[data-profile-id="prf-a"]').dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'F10',
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    await tick();
    expect(menuLabels()).toEqual([
      t('common.edit'),
      t('swarm.sidebar.startRun'),
      DELETE,
    ]);
    menuItem(t('swarm.sidebar.startRun')).click();
    await vi.waitFor(() =>
      expect(document.activeElement?.id).toBe('swarm-goal'),
    );
    expect(document.querySelector('.swarm-head')).toBeNull();
    expect(bridge.route).toBe('');
  });

  it("keeps a failed action's error through background refreshes until the next action starts", async () => {
    const { bridge, operation } = createBridge();
    let listFails = false;
    overrideOperations(operation, {
      'swarms.stop': () => {
        throw new Error('test-owned stop failure');
      },
      'swarms.list': (_args, fallback) => {
        if (listFails) throw new Error('test-owned list failure');
        return fallback();
      },
    });
    await openSwarm(bridge);
    const notice = (role) =>
      document.querySelector(`.swarm-notices [role="${role}"]`)?.textContent ??
      '';
    headButton(STOP).click();
    await vi.waitFor(() =>
      expect(notice('alert')).toContain('test-owned stop failure'),
    );
    // A failed refresh shows beside it with a way to retry ...
    listFails = true;
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(notice('status')).toContain('test-owned list failure'),
    );
    expect(button(t('common.retry'))).toBeDefined();
    expect(notice('alert')).toContain('test-owned stop failure');
    // ... and the next successful refresh clears only its own error.
    listFails = false;
    bridge.invalidate();
    await vi.waitFor(() => expect(notice('status')).toBe(''));
    expect(notice('alert')).toContain('test-owned stop failure');
    button(t('swarm.tabs.usage')).click();
    await tick();
    expect(notice('alert')).toBe('');
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
    const form = document.querySelector('.swarm-start');
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
    // The started Run shows with its title focused.
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(
        document.querySelector('.swarm-head h2'),
      ),
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

  it('ignores stale Project lookups and names blank fields at Start', async () => {
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
    // Start names each blank field at the field and focuses the first.
    button(START_RUN).click();
    await vi.waitFor(() => expect(document.activeElement).toBe(directory()));
    expect(directory().getAttribute('aria-invalid')).toBe('true');
    expect(
      document.getElementById('swarm-start-directory-error').textContent,
    ).toBe(t('swarm.start.directoryRequired'));
    expect(document.getElementById('swarm-goal-error').textContent).toBe(
      t('swarm.start.goalRequired'),
    );
    expect(callsTo(operation, 'swarms.start')).toHaveLength(0);
    // Editing a field clears its error.
    fill('swarm-start-directory', 'C:/work');
    await tick();
    expect(document.getElementById('swarm-start-directory-error')).toBeNull();
    expect(document.getElementById('swarm-goal-error')).not.toBeNull();
  });
});

describe('Swarm Run controls', () => {
  it.each([
    ['cancelled', 'the next Run', []],
    // An open Run whose participants all idle is stopped before deletion.
    ['idle', 'the next Run', ['swarms.stop']],
    // A narrow page hides the pane, so its picker takes the focus.
    ['cancelled', 'the picker of a narrow page', []],
  ])(
    'confirms deletion of a %s Run, keeps the profile, clears the selected Swarm and focuses %s',
    async (state, focus, before) => {
      const narrow = focus === 'the picker of a narrow page';
      if (narrow) {
        // jsdom lays nothing out: the pane reports itself hidden.
        Element.prototype.checkVisibility = function () {
          return !this.closest('.swarm-pane');
        };
        onTestFinished(() => delete Element.prototype.checkVisibility);
      }
      const detail = {
        ...structuredClone(swarm),
        state,
        participants: swarm.participants.map((participant) => ({
          ...structuredClone(participant),
          state: 'idle',
        })),
      };
      const { bridge, operation } = createBridge(profile, detail);
      const next = {
        id: 'swr-b',
        title: 'Second Run',
        state: 'stopped',
        participant_count: 2,
      };
      let deleted = false;
      overrideOperations(operation, {
        'swarms.stop': () => ({ swarm_id: 'swr-a', state: 'stopped' }),
        'swarms.delete': () => {
          deleted = true;
          return { deleted: true };
        },
        'swarms.list': () => ({
          entries: deleted
            ? [next]
            : [{ ...detail, participant_count: 2 }, next],
          has_more: false,
        }),
      });
      await openSwarm(bridge);
      await settle();
      await chooseRunAction(DELETE);
      expect(dialog().textContent).toContain(
        t('swarm.deleteRun.body', { title: 'Investigate' }),
      );
      // Back in the app dismisses the confirmation like Cancel.
      bridge.back();
      await tick();
      expect(dialog()).toBeNull();
      expect(callsTo(operation, 'swarms.delete')).toHaveLength(0);
      await chooseRunAction(DELETE);
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
      await vi.waitFor(() =>
        expect(document.activeElement).toBe(
          narrow ? picker() : document.querySelector('[data-run-id="swr-b"]'),
        ),
      );
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
    expect(headButton(RESUME)).toBeUndefined();
    await chooseRunAction(DELETE);
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
      expect(headButton(hidden)).toBeUndefined();
      // Deletion waits until nothing works any more, and says so.
      await openRunMenu();
      expect(menuItem(DELETE).disabled).toBe(action === 'stop');
      expect(
        menuItem(DELETE).textContent.includes(t('swarm.deleteRun.stopFirst')),
      ).toBe(action === 'stop');
      document
        .querySelector('[role="menu"]')
        .dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
        );
      await tick();
      headButton(shown).click();
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

  it('keeps failed participants visible while their peers are running and opens their Activity', async () => {
    const detail = structuredClone(swarm);
    detail.participants[1].state = 'failed';
    detail.participants.push({
      ...structuredClone(detail.participants[0]),
      id: 'prt-c',
      display_name: 'Gamma',
      state: 'interrupted',
    });
    const { bridge } = createBridge(profile, detail);
    await openSwarm(bridge);
    const banner = () => document.querySelector('.swarm-failed');
    await vi.waitFor(() => expect(banner()?.textContent).toContain('Beta'));
    expect(banner().getAttribute('role')).toBe('status');
    expect(banner().querySelector('p').textContent.trim()).toBe(
      `${t('swarm.participants.failed', { names: 'Beta and Gamma' })} ${t('swarm.participants.failedResume')}`,
    );
    // Stop stays the header's one action; the notice offers Resume.
    expect(headButton(STOP)).toBeDefined();
    expect(headButton(RESUME)).toBeUndefined();
    const actions = [...banner().querySelectorAll('button')];
    expect(actions.map((item) => item.textContent.trim())).toEqual([
      'Beta',
      'Gamma',
      RESUME,
    ]);
    actions[0].click();
    await vi.waitFor(() =>
      expect(bridge.pushRoute).toHaveBeenLastCalledWith(
        '/swarms/swr-a/participants',
      ),
    );
  });

  it('applies communication settings only on Apply, shows an invalid limit under its field, keeps a failure in the dialog and stops only on an explicit click', async () => {
    const { bridge, operation } = createBridge();
    let conflict = true;
    overrideOperations(operation, {
      'swarms.settings': () => {
        if (!conflict) return { settings_revision: 4 };
        conflict = false;
        throw new Error('test-owned settings conflict');
      },
    });
    await openSwarm(bridge);
    await tick();
    flushSync();
    await chooseRunAction(t('swarm.run.communication'));
    await choose(
      'swarm-live-delivery-main-receive',
      t('swarm.delivery.receive.pull'),
    );
    expect(callsTo(operation, 'swarms.settings')).toHaveLength(0);
    // The change reads in the fields' words.
    const changes = () =>
      dialog().querySelector('.swarm-communication__changes').textContent;
    const routeChange = t('swarm.communication.routeChange', {
      route: t('swarm.delivery.route.main'),
      setting: t('swarm.delivery.receive'),
      before: t('swarm.delivery.receive.all'),
      after: t('swarm.delivery.receive.pull'),
    });
    expect(changes()).toContain(routeChange);

    // An invalid limit shows its error under its own field only and keeps
    // Apply off; its disclosure does not close over the error.
    const disclosure = () =>
      dialog().querySelector('[aria-controls="swarm-live-delivery-advanced"]');
    const limitsShown = () =>
      !document.getElementById('swarm-live-delivery-advanced').hidden;
    const batchMessages = () =>
      document.getElementById('swarm-live-delivery-batch-messages');
    const batchError = () =>
      document.getElementById('swarm-live-delivery-batch-messages-error');
    const invalidMessage = t('swarm.communication.invalidNumber', {
      field: t('swarm.delivery.batchMessages'),
      min: '1',
      max: '100',
    });
    disclosure().click();
    await tick();
    fill('swarm-live-delivery-batch-messages', '0');
    await tick();
    expect(batchError().textContent.trim()).toBe(invalidMessage);
    expect(batchMessages().getAttribute('aria-invalid')).toBe('true');
    expect(batchMessages().getAttribute('aria-describedby')).toBe(
      'swarm-live-delivery-batch-messages-help swarm-live-delivery-batch-messages-error',
    );
    expect(changes()).toContain(routeChange);
    expect(changes()).not.toContain(invalidMessage);
    expect(button(t('swarm.apply')).disabled).toBe(true);
    // With no valid change left, the note points to the limits.
    await choose(
      'swarm-live-delivery-main-receive',
      t('swarm.delivery.receive.all'),
    );
    expect(changes()).toContain(t('swarm.communication.invalidNote'));
    expect(changes()).not.toContain(t('swarm.communication.noChanges'));
    await choose(
      'swarm-live-delivery-main-receive',
      t('swarm.delivery.receive.pull'),
    );
    disclosure().click();
    await tick();
    expect(limitsShown()).toBe(true);
    fill('swarm-live-delivery-batch-messages', '5');
    await tick();
    expect(batchError()).toBeNull();
    expect(batchMessages().getAttribute('aria-invalid')).toBeNull();
    expect(changes()).toContain(
      t('swarm.communication.change', {
        setting: t('swarm.delivery.batchMessages'),
        before: '20',
        after: '5',
      }),
    );
    expect(button(t('swarm.apply')).disabled).toBe(false);
    button(t('swarm.apply')).click();
    await settle();
    // A failed apply stays in the dialog for another try.
    expect(dialog().textContent).toContain('test-owned settings conflict');
    expect(document.querySelector('.swarm-notices')).toBeNull();
    button(t('swarm.apply')).click();
    await settle();
    expect(callsTo(operation, 'swarms.settings')).toHaveLength(2);
    expect(operation).toHaveBeenCalledWith(
      'swarms.settings',
      expect.objectContaining({
        swarm_id: 'swr-a',
        expected_revision: 3,
        delivery: expect.objectContaining({
          main: expect.objectContaining({ mode: 'pull' }),
          coalesce_ms: 250,
          batch_messages: 5,
        }),
      }),
    );
    expect(dialog()).toBeNull();
    headButton(STOP).click();
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
