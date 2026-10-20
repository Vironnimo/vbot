// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { t } from '../../lib/i18n.js';
import {
  flushSync,
  listTerminalsMock,
  startTerminalMock,
  terminalInputMock,
  terminalResizeMock,
  killTerminalMock,
  forgetTerminalMock,
  setTerminalGroupOrderMock,
  moveTerminalMock,
  subscribeTerminalEventsMock,
  createAudioRecorderMock,
  transcribeSpeechMock,
  prepareSpeechTranscriptionMock,
  streams,
  terminalInstances,
  fitAddons,
  resizeObservers,
  terminal,
  launchHistory,
  manualGroup,
  userGroup,
  terminalListResponse,
  readyAll,
  flushAnimationFrames,
  waitFor,
  findButton,
  findButtonByAriaLabel,
  setField,
  fixtureState,
  setupTerminalsViewSuite,
} from './TerminalsView.support.js';

const twoTerminals = () => [
  terminal({ terminal_id: 'term-1', title: 'First terminal' }),
  terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
];

function tileClasses() {
  return [...document.querySelectorAll('.terminals-view__tile')].map(
    (tile) => ({
      focused: tile.classList.contains('terminals-view__tile--focused'),
      maximized: tile.classList.contains('terminals-view__tile--maximized'),
      hidden: tile.classList.contains('terminals-view__tile--hidden'),
    }),
  );
}

function clickMaximize(label = 'Maximize') {
  document
    .querySelector(`button[aria-label="${label}"]`)
    .dispatchEvent(new MouseEvent('click', { bubbles: true }));
  flushSync();
}

// A layout change reaches every observed element, the tile hosts included.
function fireResize() {
  for (const observer of resizeObservers) observer.fire();
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function keydown(target, key) {
  target.dispatchEvent(
    new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }),
  );
}

function launcherInput() {
  return document.querySelector('.terminal-launcher__input');
}

function launcherError() {
  return (
    document.querySelector('.terminal-launcher__error')?.textContent.trim() ??
    ''
  );
}

function setupRows() {
  return [...document.querySelectorAll('.terminal-launcher__setup')];
}

// Each recent setup as its command line, or with the folder it names.
function setupLabels() {
  return setupRows().map((row) => {
    const command = row
      .querySelector('.terminal-launcher__setup-command')
      .textContent.trim();
    const folder = row.querySelector('.terminal-launcher__setup-folder');
    return folder ? [command, folder.textContent.trim()] : command;
  });
}

function folderChip() {
  return document
    .querySelector('.terminal-launcher__chip--folder')
    .textContent.trim();
}

function optionButton(label) {
  return [...document.querySelectorAll('[role="option"]')].find((option) =>
    option.textContent.includes(label),
  );
}

describe('TerminalsView views and starting', () => {
  const view = setupTerminalsViewSuite();

  it('initializes only the current host when a tile remounts during xterm loading', async () => {
    const { createTerminalRenderer } =
      await import('../terminals/terminalRenderer.svelte.js');
    const renderer = createTerminalRenderer({
      viewState: { streams: {} },
      findTerminal: () => terminal(),
      getController: () => ({ resize: vi.fn() }),
      isMounted: () => true,
      isUnavailable: () => false,
      getMaximizedTerminalId: () => '',
    });
    const oldHost = document.createElement('div');
    const host = document.createElement('div');
    renderer.mountTile(oldHost, 'term-1').destroy();
    renderer.mountTile(host, 'term-1');
    await vi.dynamicImportSettled();
    expect(terminalInstances).toHaveLength(1);
    expect(oldHost.children).toHaveLength(0);
    expect(host.querySelectorAll('.xterm')).toHaveLength(1);
    renderer.destroy();
    expect(terminalInstances[0].dispose).toHaveBeenCalledOnce();
  });

  const conversation = (overrides = {}) =>
    manualGroup({
      group_id: 'session:vbot/main/ses-1',
      name: 'Fix login',
      kind: 'session',
      session: { project_id: 'vbot', agent_id: 'main', session_id: 'ses-1' },
      ...overrides,
    });

  function pills() {
    return [...document.querySelectorAll('.terminals-view__pill')];
  }

  function pill(name) {
    return pills().find((button) => button.textContent.includes(name));
  }

  function menuItems() {
    return [...document.querySelectorAll('.context-menu__item')];
  }

  function dragEvent(type, transfer) {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'dataTransfer', { value: transfer });
    return event;
  }

  function dataTransfer() {
    let payload = '';
    return {
      effectAllowed: '',
      dropEffect: '',
      setData: (_kind, value) => {
        payload = value;
      },
      getData: () => payload,
    };
  }

  it('shows each conversation, group and the user’s own terminals as a pill', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [
          terminal({ group_id: conversation().group_id, state: 'working' }),
          terminal({ terminal_id: 'term-2', group_id: 'auto:manual' }),
        ],
        [
          conversation({ terminal_count: 1, live_count: 1, working_count: 1 }),
          userGroup('work', 'Work'),
          manualGroup({ terminal_count: 1, live_count: 1 }),
        ],
      ),
    );
    view.mount();
    // Only the shown view's terminals are on the canvas.
    await waitFor(() => streams.length === 1);

    expect(
      pills().map((button) => [
        button.querySelector('.terminals-view__pill-name').textContent,
        button.querySelector('.terminals-view__pill-count')?.textContent.trim(),
      ]),
    ).toEqual([
      ['Fix login', '1'],
      ['Work', undefined],
      ['Your terminals', '1'],
    ]);
    expect(pill('Fix login').getAttribute('aria-pressed')).toBe('true');
    expect(pill('Fix login').querySelector('.tab-indicator--running')).toBe(
      pill('Fix login').querySelector('.terminals-view__pill-dot'),
    );
    expect(pill('Work').querySelector('.terminals-view__pill-dot')).toBeNull();

    pill('Work').click();
    flushSync();
    expect(pill('Work').getAttribute('aria-pressed')).toBe('true');
    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
  });

  it.each([
    ['a group', 'work', { group_id: 'work' }],
    [
      'a conversation',
      'session:vbot/main/ses-1',
      { group_id: 'session:vbot/main/ses-1' },
    ],
    ['the user’s own terminals', 'auto:manual', {}],
  ])(
    'starts the default shell in %s with one click',
    async (_kind, groupId, request) => {
      listTerminalsMock.mockResolvedValue(
        terminalListResponse(
          [terminal({ group_id: groupId })],
          [
            conversation(
              groupId === 'session:vbot/main/ses-1' ? { live_count: 1 } : {},
            ),
            userGroup('work', 'Work', groupId === 'work' ? 1 : 0),
            manualGroup(groupId === 'auto:manual' ? { live_count: 1 } : {}),
          ],
        ),
      );
      startTerminalMock.mockResolvedValue({
        terminal: terminal({ terminal_id: 'new-1', group_id: groupId }),
      });
      view.mount();
      await waitFor(() => streams.length === 1);

      findButtonByAriaLabel('New terminal').click();

      await waitFor(() => document.querySelector('[data-terminal-id="new-1"]'));
      expect(startTerminalMock).toHaveBeenCalledWith(request);
      expect(document.querySelector('.terminal-launcher')).toBeNull();
      const tiles = document.querySelectorAll('.terminals-view__tile');
      expect(tiles[tiles.length - 1].dataset.terminalId).toBe('new-1');
    },
  );

  it('opens the launcher from the start button with recent setups and a new group', async () => {
    listTerminalsMock.mockResolvedValue({
      ...terminalListResponse([]),
      launch_history: [
        launchHistory({
          id: 'codex',
          command: 'codex',
          args: ['--profile', 'daily'],
          workdir: 'C:\\repo',
        }),
      ],
    });
    startTerminalMock.mockResolvedValue({ terminal: terminal() });
    view.mount();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );

    const chevron = findButtonByAriaLabel('More ways to start a terminal');
    chevron.click();
    await waitFor(() => document.activeElement === launcherInput());
    expect(chevron.getAttribute('aria-expanded')).toBe('true');
    expect(setupLabels()).toEqual(['codex --profile daily']);

    setupRows()[0].click();
    await waitFor(() => startTerminalMock.mock.calls.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({
      command: 'codex',
      args: ['--profile', 'daily'],
      workdir: 'C:\\repo',
    });
    await waitFor(() => !document.querySelector('.terminal-launcher'));

    chevron.click();
    await waitFor(() => document.querySelector('.terminal-launcher'));
    keydown(launcherInput(), 'Escape');
    flushSync();
    expect(document.querySelector('.terminal-launcher')).toBeNull();
    expect(document.activeElement).toBe(chevron);

    chevron.click();
    await waitFor(() => document.querySelector('.terminal-launcher'));
    findButton('New group…').click();
    flushSync();
    expect(document.querySelector('.terminal-launcher')).toBeNull();
    expect(document.querySelector('#terminal-group-form')).toBeTruthy();
  });

  it('disables starting when the server is unavailable', () => {
    view.mount({ serverUnavailable: true });
    expect(findButtonByAriaLabel('New terminal').disabled).toBe(true);
    expect(
      findButtonByAriaLabel('More ways to start a terminal').disabled,
    ).toBe(true);
  });

  it('renders the shared empty state when no Terminal Session is active', async () => {
    listTerminalsMock.mockResolvedValue({ groups: [], terminals: [] });
    view.mount();
    await waitFor(
      () =>
        !document.body.textContent.includes('Loading terminal sessions') &&
        document.querySelector('.terminals-view__detail > .empty-state'),
    );

    const emptyState = document.querySelector(
      '.terminals-view__detail > .empty-state',
    );
    expect(
      [...emptyState.querySelectorAll('button')].map((button) =>
        button.textContent.trim(),
      ),
    ).toEqual(['New terminal', 'Customize…']);
    expect(pills()).toHaveLength(0);
    expect(subscribeTerminalEventsMock).not.toHaveBeenCalled();
  });

  it('offers rename and delete for a group and opening the conversation for a conversation', async () => {
    const onOpenSession = vi.fn();
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal({ group_id: 'work' })],
        [userGroup('work', 'Work', 1), conversation({ live_count: 1 })],
      ),
    );
    view.mount({ onOpenSession });
    await waitFor(() => streams.length === 1);

    pill('Work').dispatchEvent(
      new MouseEvent('contextmenu', { bubbles: true, cancelable: true }),
    );
    await waitFor(() => menuItems().length > 0);
    expect(menuItems().map((item) => item.textContent.trim())).toEqual([
      'Rename group…',
      'Delete group…',
    ]);
    expect(menuItems()[1].classList).toContain('context-menu__item--danger');
    menuItems()[1].click();
    flushSync();
    expect(document.querySelector('.modal-title').textContent).toBe(
      'Delete group “Work”?',
    );
    expect(document.body.textContent).toContain(
      t('terminals.deleteGroupWarning'),
    );
    findButton('Cancel').click();
    flushSync();

    pill('Fix login').click();
    flushSync();
    findButtonByAriaLabel('Open conversation').click();
    expect(onOpenSession).toHaveBeenCalledWith('main@vbot', 'ses-1');
  });

  it('reorders the terminals of a view by drag and drop on the tile bar', async () => {
    await view.mountWith(twoTerminals());
    const bars = document.querySelectorAll('.terminals-view__tile-bar');
    expect(bars[0].getAttribute('draggable')).toBe('true');

    const transfer = dataTransfer();
    bars[0].dispatchEvent(dragEvent('dragstart', transfer));
    bars[1].dispatchEvent(dragEvent('dragover', transfer));
    bars[1].dispatchEvent(dragEvent('drop', transfer));
    flushSync();

    expect(setTerminalGroupOrderMock).toHaveBeenCalledWith('auto:manual', [
      'term-2',
      'term-1',
    ]);
    expect(
      document
        .querySelector('.terminals-view__tile')
        .getAttribute('data-terminal-id'),
    ).toBe('term-2');
  });

  it('moves a terminal to another view by dropping its tile on a pill or from its menu', async () => {
    const groups = [
      manualGroup({ terminal_count: 2, live_count: 2 }),
      userGroup('work', 'Work'),
    ];
    await view.mountWith(twoTerminals(), groups);
    moveTerminalMock.mockResolvedValue({
      terminal: terminal({ group_id: 'work', pinned: true }),
    });
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal({ group_id: 'work', pinned: true }), twoTerminals()[1]],
        [
          manualGroup({ terminal_count: 1, live_count: 1 }),
          userGroup('work', 'Work', 1),
        ],
      ),
    );

    const transfer = dataTransfer();
    document
      .querySelector('.terminals-view__tile-bar')
      .dispatchEvent(dragEvent('dragstart', transfer));
    pill('Work').dispatchEvent(dragEvent('dragover', transfer));
    flushSync();
    expect(pill('Work').classList).toContain(
      'terminals-view__pill--drop-target',
    );
    pill('Work').dispatchEvent(dragEvent('drop', transfer));
    flushSync();

    // The tile leaves the shown view at once.
    expect(
      [...document.querySelectorAll('.terminals-view__tile')].map(
        (tile) => tile.dataset.terminalId,
      ),
    ).toEqual(['term-2']);
    expect(moveTerminalMock).toHaveBeenCalledWith('term-1', 'work');

    pill('Work').click();
    await waitFor(() => document.querySelector('[data-terminal-id="term-1"]'));
    expect(document.querySelector('.terminals-view__tile-pin')).toBeTruthy();
    document
      .querySelector('.terminals-view__tile-bar')
      .dispatchEvent(
        new MouseEvent('contextmenu', { bubbles: true, cancelable: true }),
      );
    await waitFor(() => menuItems().length > 0);
    expect(menuItems().map((item) => item.textContent.trim())).toEqual([
      'Show where it started',
      'Maximize',
      'Close terminal',
    ]);
    menuItems()[0].click();
    expect(moveTerminalMock).toHaveBeenLastCalledWith('term-1', null);
  });
});

describe('TerminalsView launch and close', () => {
  const view = setupTerminalsViewSuite();

  async function openLauncher() {
    view.mount();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );
    findButton('Customize…').click();
    await waitFor(() => document.activeElement === launcherInput());
  }

  it('starts a typed command line in the chosen folder and view and focuses it', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [],
        [manualGroup(), userGroup('work', 'Work'), userGroup('ops', 'Ops')],
      ),
    );
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'manual-1',
        group_id: 'ops',
        command: 'codex',
        owner: null,
      }),
    });
    await openLauncher();

    setField('.terminal-launcher__input', 'codex --profile "work space"');
    findButtonByAriaLabel('Working directory: Home directory').click();
    await waitFor(() =>
      document.activeElement?.closest('.terminal-launcher__path'),
    );
    setField('.terminal-launcher__path input', 'C:\\repo');
    keydown(document.activeElement, 'Enter');
    await waitFor(() => document.activeElement === launcherInput());
    expect(folderChip()).toBe('repo');

    // It shows in the shown view unless another one is chosen.
    findButtonByAriaLabel('Show in: Work').click();
    await waitFor(() => optionButton('Ops'));
    optionButton('Ops').click();
    flushSync();
    keydown(launcherInput(), 'Enter');

    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({
      command: 'codex',
      args: ['--profile', 'work space'],
      workdir: 'C:\\repo',
      group_id: 'ops',
    });
    expect(document.querySelector('.terminal-launcher')).toBeNull();
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();
  });

  it('shows start errors at the command line and starts the default shell when cleared', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([]));
    startTerminalMock
      .mockRejectedValueOnce(new Error('The program nope was not found.'))
      .mockResolvedValue({ terminal: terminal({ owner: null }) });
    await openLauncher();

    setField('.terminal-launcher__input', 'codex "unfinished');
    keydown(launcherInput(), 'Enter');
    flushSync();
    expect(launcherInput().getAttribute('aria-invalid')).toBe('true');
    expect(launcherError()).toBe(t('terminals.commandError.unclosedQuote'));
    expect(startTerminalMock).not.toHaveBeenCalled();
    expect(launcherInput().value).toBe('codex "unfinished');

    setField('.terminal-launcher__input', 'nope');
    flushSync();
    expect(launcherError()).toBe('');
    keydown(launcherInput(), 'Enter');
    await waitFor(() => launcherError() !== '');
    expect(launcherError()).toBe('The program nope was not found.');
    expect(document.activeElement).toBe(launcherInput());

    setField('.terminal-launcher__input', '   ');
    flushSync();
    expect(launcherError()).toBe('');
    keydown(launcherInput(), 'Enter');
    await waitFor(() => startTerminalMock.mock.calls.length === 2);
    expect(startTerminalMock).toHaveBeenLastCalledWith({});
  });

  it('filters the recent setups as you type and starts the chosen one', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [],
      terminals: [],
      launch_history: [
        launchHistory({
          id: 'recent',
          command: 'codex',
          args: ['--profile', 'daily'],
          workdir: 'C:\\Development\\vBot',
        }),
        launchHistory({ id: 'shell', workdir: 'C:\\Development\\vBot' }),
        launchHistory({ id: 'home-shell' }),
        launchHistory({
          id: 'older',
          command: 'python',
          args: ['-m', 'http.server', '8080'],
          workdir: 'C:\\Sites\\docs',
        }),
      ],
    });
    startTerminalMock.mockResolvedValue({ terminal: terminal() });
    await openLauncher();

    // The last used folder is the default; the empty command line already
    // starts the default shell there, and only other folders are named.
    expect(folderChip()).toBe('vBot');
    expect(setupLabels()).toEqual([
      'codex --profile daily',
      ['Default shell', 'Home directory'],
      ['python -m http.server 8080', 'C:\\Sites\\docs'],
    ]);

    // Tab moves a setup into the launcher to edit it.
    keydown(launcherInput(), 'ArrowUp');
    keydown(launcherInput(), 'Tab');
    flushSync();
    expect(launcherInput().value).toBe('python -m http.server 8080');
    expect(folderChip()).toBe('docs');

    setField('.terminal-launcher__input', 'cod');
    flushSync();
    expect(setupLabels()).toEqual([
      ['codex --profile daily', 'C:\\Development\\vBot'],
    ]);
    keydown(launcherInput(), 'ArrowDown');
    flushSync();
    expect(launcherInput().getAttribute('aria-activedescendant')).toBe(
      setupRows()[0].id,
    );
    expect(setupRows()[0].getAttribute('aria-selected')).toBe('true');
    keydown(launcherInput(), 'Enter');
    await waitFor(() => startTerminalMock.mock.calls.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({
      command: 'codex',
      args: ['--profile', 'daily'],
      workdir: 'C:\\Development\\vBot',
    });
  });

  it.each([
    ['running', {}, true],
    ['finished', { state: 'exited', exit_code: 0 }, false],
  ])(
    'closes a %s Terminal Session with one click and removes its tile at once',
    async (_kind, changes, stops) => {
      // A terminal that finishes while shown stays until it is closed.
      await view.mountWith([terminal()]);
      readyAll([terminal(changes)], '\u001b[2Jretained final answer');
      expect(document.querySelector('.terminals-view__exit-code')).toBeNull();

      findButtonByAriaLabel('Close terminal').click();
      flushSync();

      // The server-side stop and catalog removal continue in the background,
      // without a confirmation dialog.
      expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(
        0,
      );
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      await waitFor(() => forgetTerminalMock.mock.calls.length > 0);
      expect(forgetTerminalMock).toHaveBeenCalledWith('term-1');
      expect(killTerminalMock.mock.calls).toEqual(stops ? [['term-1']] : []);
    },
  );

  it('keeps a finished Terminal Session available as read-only history', async () => {
    const finished = terminal({
      state: 'exited',
      exit_code: 1,
      finished_at: '2026-08-03T12:05:00+00:00',
      launch_command: 'npm',
      launch_args: ['run', 'build'],
    });
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([finished], [manualGroup({ terminal_count: 1 })]),
    );
    view.mount();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );
    // A terminal that was finished before it was shown waits in the history.
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(0);
    expect(
      document.querySelector('.terminals-view__history-count').textContent,
    ).toBe('1');

    findButtonByAriaLabel('Finished terminals').click();
    await waitFor(() => document.querySelector('.context-menu__item'));
    const entry = document.querySelector('.context-menu__item');
    expect(entry.querySelector('.context-menu__label').textContent).toBe(
      'Anna · npm run build',
    );
    expect(entry.querySelector('.context-menu__hint').textContent).toBe(
      'Your terminals',
    );
    entry.click();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    readyAll([finished], '\u001b[2Jretained final answer');

    expect(
      document.querySelector('.terminals-view__history-bar').textContent,
    ).toContain('Anna · npm run build');
    expect(findButtonByAriaLabel('Close terminal')).toBeTruthy();
    expect(terminalInstances[0].options.disableStdin).toBe(true);

    // The keyboard-focusable title bar exposes the details card.
    document.querySelector('.terminals-view__tile-bar').focus();
    await vi.waitFor(() =>
      expect(document.getElementById('app-tooltip')?.dataset.floatingOpen).toBe(
        'true',
      ),
    );
    const card = document.getElementById('app-tooltip');
    const rows = Object.fromEntries(
      [...card.querySelectorAll('dt')].map((term) => [
        term.textContent,
        term.nextElementSibling,
      ]),
    );
    expect(rows[t('terminals.details.command')].textContent).toBe(
      'npm run build',
    );
    expect(rows[t('terminals.details.directory')].textContent).toBe(
      'C:\\Development\\vBot',
    );
    const state = rows[t('terminals.details.state')];
    expect(state.textContent).toBe(
      t('terminals.state.exitedWithCode', { code: 1 }),
    );
    expect(state.classList).toContain('app-tooltip__value--danger');
    expect(rows[t('terminals.details.finished')].textContent).toContain(' · ');
    // A finished process has no process id any more.
    expect(rows).not.toHaveProperty(t('terminals.details.pid'));

    // A pill names what the view is and counts only the kinds it holds.
    document.querySelector('.terminals-view__pill').focus();
    flushSync();
    expect(card.querySelector('.app-tooltip__text').textContent).toBe(
      t('terminals.groupHint.manual'),
    );
    expect(card.querySelector('dd').textContent).toBe(
      t('terminals.details.terminalCountFinished', { count: 1 }),
    );

    findButton('Back').click();
    flushSync();
    expect(document.querySelector('.terminals-view__history-bar')).toBeNull();
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(0);
  });
});

describe('TerminalsView rendering and input', () => {
  const view = setupTerminalsViewSuite();

  it('renders tiles with title, owner and compact actions from the live snapshot without owning their lifetime', async () => {
    const shell = terminal({
      terminal_id: 'term-2',
      command: 'pwsh.exe',
      title: '',
    });
    await view.mountWith([terminal(), shell]);
    readyAll(
      [
        terminal({
          title: 'Auth refactor · Codex',
          attention: {
            revision: 1,
            kind: 'idle',
            summary: 'The program is idle.',
          },
        }),
        shell,
      ],
      '\u001b[2JDemo TUI ready',
    );
    await flushAnimationFrames(2);

    const bars = document.querySelectorAll('.terminals-view__tile-bar');
    expect(bars).toHaveLength(2);
    expect(bars[0].textContent).toContain('Anna');
    expect(bars[0].textContent).toContain('Auth refactor · Codex');
    // Before a program announces a title, the default shell is humanized.
    expect(bars[1].textContent).toContain('PowerShell');
    const text = document.body.textContent;
    for (const hidden of [
      'python',
      'pwsh.exe',
      'PID 4321',
      'Quiet is not a semantic prompt.',
    ]) {
      expect(text).not.toContain(hidden);
    }
    expect(
      bars[0].querySelectorAll('.terminals-view__tile-action'),
    ).toHaveLength(3);
    expect(
      bars[0]
        .querySelector('.terminals-view__tile-action svg')
        .getAttribute('width'),
    ).toBe('14');
    expect(document.querySelector('.terminals-view__tile-bar-meta')).toBeNull();
    expect(document.querySelector('button[role="switch"]')).toBeNull();
    expect(
      document.querySelector('button[aria-label="Take control"]'),
    ).toBeNull();

    // The DOM renderer only: FitAddon is the single addon, fitting the host.
    expect(terminalInstances[0].options.theme.background).toBe('#0E0D0B');
    expect(terminalInstances[0].loadAddon.mock.calls).toEqual([[fitAddons[0]]]);
    expect(fitAddons[0].fit).toHaveBeenCalled();
    expect([terminalInstances[0].cols, terminalInstances[0].rows]).toEqual([
      100, 32,
    ]);
    // The snapshot carries its own in-band reset (RIS).
    expect(terminalInstances[0].write).toHaveBeenCalledWith(
      '\u001bc\u001b[2JDemo TUI ready',
      expect.any(Function),
    );

    await view.unmount();
    for (const stream of streams) {
      expect(stream.connection.close).toHaveBeenCalledWith(
        1000,
        'terminals-view-close',
      );
    }
    expect(killTerminalMock).not.toHaveBeenCalled();
  });

  it('rebuilds every tile from its retained snapshot when the Terminals tab is mounted again', async () => {
    await view.mountWith(twoTerminals());
    await view.unmount();

    view.mount();
    await waitFor(() => streams.length === 4 && terminalInstances.length === 4);
    const snapshots = [
      '\u001b[2J\u001b[Hhistory-0\r\nhistory-1\r\nfirst screen',
      '\u001b[2Jsecond remount snapshot',
    ];
    // The snapshots carry the grid the tiles already fit (100x32).
    twoTerminals().forEach((item, index) =>
      streams[2 + index].handlers.onEvent({
        type: 'terminal_ready',
        sequence: 4,
        terminal: { ...item, columns: 100, rows: 32 },
        ansi: snapshots[index],
      }),
    );
    flushSync();

    expect(terminalInstances[2].write).toHaveBeenCalledWith(
      `\u001bc${snapshots[0]}`,
      expect.any(Function),
    );
    expect(terminalInstances[3].write).toHaveBeenCalledWith(
      `\u001bc${snapshots[1]}`,
      expect.any(Function),
    );
  });

  it('focuses the clicked tile, routes native keys to it, and exposes scrollback recovery', async () => {
    await view.mountWith(twoTerminals());
    readyAll(twoTerminals());
    expect(tileClasses().map((tile) => tile.focused)).toEqual([true, false]);

    document
      .querySelectorAll('.terminals-view__tile-bar')[1]
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(tileClasses().map((tile) => tile.focused)).toEqual([false, true]);
    expect(terminalInstances[1].focus).toHaveBeenCalled();

    // Every running tile stays writable; a click on its host focuses it.
    document
      .querySelectorAll('.terminals-view__tile-host')[0]
      .dispatchEvent(
        new MouseEvent('pointerdown', { bubbles: true, button: 0 }),
      );
    flushSync();
    expect(tileClasses().map((tile) => tile.focused)).toEqual([true, false]);
    expect(terminalInstances[0].focus).toHaveBeenCalled();
    expect(
      terminalInstances.map((instance) => instance.options.disableStdin),
    ).toEqual([false, false]);

    // Focus reports from xterm never reach the program.
    for (const key of ['\u001b[A', '\u001b[I', '\u001b[O', '\r']) {
      terminalInstances[0].onDataCallback(key);
    }
    terminalInstances[1].onDataCallback('ls');
    await wait(30);
    expect(terminalInputMock).toHaveBeenCalledWith('term-1', '\u001b[A\r');
    expect(terminalInputMock).toHaveBeenCalledWith('term-2', 'ls');

    terminalInstances[0].buffer.active = { viewportY: 3, baseY: 12 };
    terminalInstances[0].onScrollCallback();
    flushSync();
    findButton('Jump to latest').click();
    expect(terminalInstances[0].scrollToBottom).toHaveBeenCalledTimes(1);
  });

  it('suppresses viewer protocol replies while preserving title and color changes', async () => {
    await view.mountWith([terminal()]);
    const parser = terminalInstances[0].parser;
    for (const [id, handler] of parser.registerCsiHandler.mock.calls) {
      if (id.final === 't') {
        expect(handler([18])).toBe(true);
        expect(handler([22])).toBe(false);
      } else {
        expect(handler([6])).toBe(true);
      }
    }
    for (const [, handler] of parser.registerOscHandler.mock.calls) {
      expect(handler('?')).toBe(true);
      expect(handler('rgb:0000/0000/0000')).toBe(false);
    }
    expect(parser.registerDcsHandler.mock.calls[0][1]('m')).toBe(true);
    expect(terminalInputMock).not.toHaveBeenCalled();
  });

  it('maximizes a tile, resizes its PTY at once, keeps it streaming, and restores the grid', async () => {
    await view.mountWith(twoTerminals());
    readyAll(twoTerminals());
    await waitFor(() => fitAddons[0].fit.mock.calls.length > 0);
    const canvas = document.querySelector('.terminals-view__canvas');
    expect(canvas.getAttribute('style')).toContain('repeat(2, minmax(0, 1fr))');

    clickMaximize();
    expect(canvas.getAttribute('style')).toContain('repeat(1, minmax(0, 1fr))');
    expect(canvas.classList.contains('terminals-view__canvas--maximized')).toBe(
      true,
    );
    expect(tileClasses()).toEqual([
      { focused: false, maximized: true, hidden: false },
      { focused: false, maximized: false, hidden: true },
    ]);
    // Maximize is a deterministic user action: its fit resizes the PTY
    // without the debounce.
    await waitFor(() => terminalResizeMock.mock.calls.length > 0);
    expect(terminalResizeMock).toHaveBeenCalledWith('term-1', 100, 32);

    streams[0].handlers.onEvent({
      type: 'terminal_output',
      sequence: 2,
      data: 'more output',
    });
    expect(terminalInstances[0].write).toHaveBeenCalledWith('more output');

    clickMaximize('Restore');
    expect(canvas.getAttribute('style')).toContain('repeat(2, minmax(0, 1fr))');
    expect(canvas.classList.contains('terminals-view__canvas--maximized')).toBe(
      false,
    );
    expect(tileClasses().some((tile) => tile.maximized || tile.hidden)).toBe(
      false,
    );
    expect(terminalInstances).toHaveLength(2);
  });

  it('fits a resized tile at the base font and debounces it into the PTY', async () => {
    await view.mountWith([terminal()]);
    readyAll([terminal()]);
    await wait(150);
    const xterm = terminalInstances[0];
    terminalResizeMock.mockClear();

    // 300 columns at the base font exceed the PTY limit: the font grows.
    fixtureState.mockHostWidth = 2400;
    fireResize();
    await wait(120);
    expect(xterm.options.fontSize).toBeGreaterThan(12);
    expect(terminalResizeMock).toHaveBeenLastCalledWith('term-1', 240, 25);

    // Shrinking again returns to the base font instead of keeping it large.
    fixtureState.mockHostWidth = 400;
    fireResize();
    await Promise.resolve();
    flushSync();
    expect(xterm.options.fontSize).toBe(12);
    expect(terminalResizeMock).toHaveBeenCalledTimes(1);
    await wait(120);
    expect(terminalResizeMock).toHaveBeenLastCalledWith('term-1', 50, 32);
  });

  it('re-fits the first tile when a second terminal joins the canvas', async () => {
    startTerminalMock.mockResolvedValue({
      terminal: terminal({ terminal_id: 'term-2', command: 'opencode' }),
    });
    await view.mountWith([terminal()]);
    const fitsBefore = fitAddons[0].fit.mock.calls.length;

    findButtonByAriaLabel('New terminal').click();
    await waitFor(() => terminalInstances.length === 2);
    await Promise.resolve();
    flushSync();
    await flushAnimationFrames(1);

    expect(fitAddons[0].fit.mock.calls.length).toBeGreaterThan(fitsBefore);
  });

  it('mirrors a grid another viewer set until this tile is used again', async () => {
    await view.mountWith([terminal()]);
    readyAll([terminal()]);
    await wait(150);
    const xterm = terminalInstances[0];
    expect(terminalResizeMock).toHaveBeenCalledWith('term-1', 100, 32);
    terminalResizeMock.mockClear();

    // Output is laid out for the PTY grid, so the tile renders that grid.
    streams[0].handlers.onEvent({
      type: 'terminal_state',
      sequence: 2,
      terminal: terminal({ columns: 90, rows: 30 }),
    });
    await wait(120);
    expect([xterm.cols, xterm.rows]).toEqual([90, 30]);
    expect(xterm.options.fontSize).toBe(12);
    expect(terminalResizeMock).not.toHaveBeenCalled();

    // A grid larger than the tile shrinks the font instead of clipping.
    streams[0].handlers.onEvent({
      type: 'terminal_state',
      sequence: 3,
      terminal: terminal({ columns: 200, rows: 40 }),
    });
    await wait(120);
    expect([xterm.cols, xterm.rows]).toEqual([200, 40]);
    expect(xterm.options.fontSize).toBeLessThanOrEqual(6);
    expect(xterm.options.fontSize).toBeGreaterThanOrEqual(4);
    expect(terminalResizeMock).not.toHaveBeenCalled();

    // Typing makes this tile the one the PTY follows again.
    xterm.onDataCallback('x');
    await wait(120);
    expect([xterm.cols, xterm.rows]).toEqual([100, 32]);
    expect(xterm.options.fontSize).toBe(12);
    expect(terminalResizeMock).toHaveBeenCalledWith('term-1', 100, 32);
  });

  it('keeps the live tiles while a list refresh fails', async () => {
    const component = await view.mountWith([terminal()]);
    listTerminalsMock.mockRejectedValue(new Error('offline'));

    await expect(component.applyVoiceAction('refresh')).rejects.toThrow(
      'terminal_refresh_failed',
    );
    flushSync();

    expect(document.body.textContent).toContain(t('terminals.listError'));
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(1);
    expect(terminalInstances[0].dispose).not.toHaveBeenCalled();
  });
});

describe('TerminalsView voice and dictation', () => {
  const view = setupTerminalsViewSuite();

  it('lets voice maximize, restore and show a group without touching the terminals', async () => {
    const component = await view.mountWith(twoTerminals(), [
      manualGroup({ terminal_count: 2, live_count: 2 }),
      userGroup('review', 'Review'),
    ]);
    const context = () => component.getVoiceContext();

    await component.applyVoiceAction('maximize', { terminal_id: 'term-2' });
    flushSync();
    expect(context()).toMatchObject({
      maximized_terminal_id: 'term-2',
      selected_terminal_id: 'term-2',
    });
    await component.applyVoiceAction('restore');
    flushSync();
    expect(context()).toMatchObject({
      maximized_terminal_id: '',
      visible_order: ['term-1', 'term-2'],
    });

    // Showing another group clears a maximize and may show an empty group.
    await component.applyVoiceAction('maximize', { terminal_id: 'term-1' });
    await component.applyVoiceAction('show_group', { group_id: 'review' });
    flushSync();
    expect(context()).toMatchObject({
      selected_group_id: 'review',
      maximized_terminal_id: '',
      visible_order: [],
    });
    expect(document.querySelector('.terminal-tile')).toBeNull();
    await expect(
      component.applyVoiceAction('show_group', { group_id: 'missing' }),
    ).rejects.toThrow('group_not_found');
    expect(context().selected_group_id).toBe('review');
    expect(killTerminalMock).not.toHaveBeenCalled();
    expect(terminalInputMock).not.toHaveBeenCalled();
  });

  it('records from the tile bar and pastes through xterm into the original terminal without Enter', async () => {
    const recorder = {
      start: vi.fn(),
      cancel: vi.fn(),
      stop: vi.fn().mockResolvedValue(new Blob(['audio'])),
      filename: () => 'recording.webm',
    };
    createAudioRecorderMock.mockResolvedValue(recorder);
    let finishTranscription;
    transcribeSpeechMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishTranscription = resolve;
        }),
    );
    await view.mountWith(twoTerminals());
    readyAll(twoTerminals());
    const [microphone, otherMicrophone] = ['term-1', 'term-2'].map((id) =>
      document.querySelector(
        `[data-terminal-id="${id}"] button[aria-label="Dictate into terminal"]`,
      ),
    );

    microphone.click();
    await waitFor(() => microphone.getAttribute('aria-pressed') === 'true');
    expect(recorder.start).toHaveBeenCalledOnce();
    // The server may load its speech model while the user speaks.
    expect(prepareSpeechTranscriptionMock).toHaveBeenCalledOnce();
    expect(otherMicrophone.disabled).toBe(true);

    microphone.click();
    await waitFor(() => transcribeSpeechMock.mock.calls.length === 1);
    expect(microphone.getAttribute('aria-busy')).toBe('true');
    document
      .querySelector('[data-terminal-id="term-2"] .terminals-view__tile-bar')
      .click();
    terminalInstances.forEach((instance) => instance.focus.mockClear());
    finishTranscription({
      text: '  Bitte prüfen\r\nund ergänzen.\t\u001b\u0003  ',
    });

    await waitFor(() => terminalInstances[0].paste.mock.calls.length === 1);
    expect(terminalInstances[0].paste).toHaveBeenCalledWith(
      'Bitte prüfen und ergänzen.',
    );
    expect(terminalInstances[1].paste).not.toHaveBeenCalled();
    expect(terminalInstances[0].focus).not.toHaveBeenCalled();
    await wait(30);
    expect(terminalInputMock).toHaveBeenCalledWith(
      'term-1',
      'Bitte prüfen und ergänzen.',
    );
    expect(microphone.getAttribute('aria-pressed')).toBe('false');
  });
});
