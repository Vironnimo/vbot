// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  listTerminalsMock,
  startTerminalMock,
  sendTerminalInputMock,
  resizeTerminalMock,
  killTerminalMock,
  forgetTerminalMock,
  setTerminalGroupOrderMock,
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
  submitStartForm,
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

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

describe('TerminalsView groups and layout', () => {
  const view = setupTerminalsViewSuite();

  it('uses the compact top group bar and keeps both terminal actions available', async () => {
    await view.mountWith([terminal()]);

    const toolbar = document.querySelector('.terminals-view__toolbar');
    const tabs = toolbar.querySelector('.terminals-view__group-tabs');
    const item = tabs.querySelector('.terminals-view__group-tab');
    expect(document.querySelector('.terminals-view__list-pane')).toBeNull();
    expect(
      tabs.lastElementChild.querySelector('button[aria-label="Add group"]'),
    ).toBeTruthy();
    expect(item.classList.contains('active')).toBe(true);
    expect(item.getAttribute('aria-current')).toBe('true');
    expect(
      item.parentElement.querySelector('button[aria-label="New terminal"]'),
    ).toBeTruthy();
    expect(
      toolbar.querySelector('.terminals-view__toolbar-actions'),
    ).toBeNull();

    findButtonByAriaLabel('Add group').click();
    flushSync();
    expect(document.querySelector('#terminal-group-form')).toBeTruthy();
    expect(document.querySelector('.terminals-view__header')).toBeNull();
  });

  // The append tile shares the last tile's row: inside the last cell when
  // that row is full, in the next free column otherwise.
  it.each([
    [1, 1, true],
    [3, 2, true],
    [5, 2, false],
    [8, 2, true],
    [9, 3, false],
  ])(
    'places the narrow append tile after %i terminals in %i rows',
    async (count, rows, sharesCell) => {
      await view.mountWith(
        Array.from({ length: count }, (_, index) =>
          terminal({ terminal_id: `term-${index}` }),
        ),
      );
      const canvas = document.querySelector('.terminals-view__canvas');
      const tiles = [...canvas.querySelectorAll('.terminals-view__tile')];
      const append = canvas.querySelector('.terminals-view__append-tile');
      expect(canvas.lastElementChild).toBe(append);
      expect(append.style.gridRow).toBe(
        tiles.at(-1).style.gridRow.split(' / ')[0],
      );
      expect(canvas.style.gridTemplateRows).toBe(
        `repeat(${rows}, minmax(0, 1fr))`,
      );
      expect(
        append.classList.contains('terminals-view__append-tile--shared'),
      ).toBe(sharesCell);
      expect(
        tiles.map((tile) =>
          tile.classList.contains('terminals-view__tile--append-space'),
        ),
      ).toEqual(tiles.map((_, index) => sharesCell && index === count - 1));

      append.click();
      flushSync();
      expect(document.querySelector('#terminal-start-form')).toBeTruthy();
    },
  );

  it.each([
    '.terminals-view__group-start button',
    '.terminals-view__append-tile',
    '.empty-state__actions button',
  ])('starts in the selected group from %s', async (selector) => {
    const empty = selector.includes('empty-state');
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(empty ? [] : [terminal({ group_id: 'work' })], [
        userGroup('work', 'Work', empty ? 0 : 1),
      ]),
    );
    startTerminalMock.mockResolvedValue({
      terminal: terminal({ terminal_id: 'new-work', group_id: 'work' }),
    });
    view.mount();
    await waitFor(() => document.querySelector(selector));

    document.querySelector(selector).click();
    flushSync();
    submitStartForm();

    await waitFor(() => startTerminalMock.mock.calls.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({ group_id: 'work' });
    await waitFor(() =>
      document.querySelector('[data-terminal-id="new-work"]'),
    );
    const append = document.querySelector('.terminals-view__append-tile');
    expect(append.previousElementSibling.dataset.terminalId).toBe('new-work');
  });

  it('disables both creation locations when the server is unavailable', () => {
    view.mount({ serverUnavailable: true });
    expect(findButtonByAriaLabel('Add group').disabled).toBe(true);
    expect(findButton('New terminal').disabled).toBe(true);
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
      [...emptyState.querySelectorAll('button')].some(
        (button) => button.textContent.trim() === 'New terminal',
      ),
    ).toBe(true);
    // The centred empty state is the only absence message: the toolbar keeps
    // its icon-only add control without repeating it as text.
    expect(
      document.querySelector('.terminals-view__toolbar').textContent.trim(),
    ).toBe('');
    expect(subscribeTerminalEventsMock).not.toHaveBeenCalled();
  });

  it('switches the visible terminal group from the top group bar', async () => {
    await view.mountWith(
      [terminal()],
      [
        manualGroup({ terminal_count: 1, live_count: 1 }),
        userGroup('group-work', 'Work'),
      ],
    );
    const workTab = () =>
      [...document.querySelectorAll('.terminals-view__group-tab')].find(
        (button) => button.textContent.includes('Work'),
      );

    workTab().dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(workTab().getAttribute('aria-current')).toBe('true');
    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
  });

  it('offers rename and delete for a user group through one action menu', async () => {
    await view.mountWith(
      [terminal({ group_id: 'group-work' })],
      [userGroup('group-work', 'Work', 1)],
    );
    const trigger = document.querySelector(
      '.terminals-view__group-action-menu-trigger',
    );
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');
    expect(document.querySelector('.terminals-view__group-menu')).toBeNull();

    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await waitFor(() => document.querySelector('.terminals-view__group-menu'));

    const items = [
      ...document.querySelectorAll('.terminals-view__group-menu-item'),
    ];
    expect(items.map((item) => item.textContent.trim())).toEqual([
      'Rename group',
      'Delete group',
    ]);
    expect(items[1].classList).toContain(
      'terminals-view__group-menu-item--danger',
    );
  });

  it('reorders terminals in a user group by drag and drop on the tile bar', async () => {
    await view.mountWith(
      twoTerminals().map((item) => ({ ...item, group_id: 'group-1' })),
      [userGroup('group-1', 'Work', 2)],
    );
    const bars = document.querySelectorAll('.terminals-view__tile-bar');
    expect(bars[0].getAttribute('draggable')).toBe('true');

    let payload = '';
    function dragEvent(type) {
      const event = new MouseEvent(type, { bubbles: true, cancelable: true });
      Object.defineProperty(event, 'dataTransfer', {
        value: {
          effectAllowed: '',
          dropEffect: '',
          setData: (_kind, value) => {
            payload = value;
          },
          getData: () => payload,
        },
      });
      return event;
    }
    bars[0].dispatchEvent(dragEvent('dragstart'));
    bars[1].dispatchEvent(dragEvent('dragover'));
    bars[1].dispatchEvent(dragEvent('drop'));
    flushSync();

    expect(setTerminalGroupOrderMock).toHaveBeenCalledWith('group-1', [
      'term-2',
      'term-1',
    ]);
    expect(
      document
        .querySelector('.terminals-view__tile')
        .getAttribute('data-terminal-id'),
    ).toBe('term-2');
  });
});

describe('TerminalsView launch and close', () => {
  const view = setupTerminalsViewSuite();

  async function openStartDialog() {
    view.mount();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );
    findButtonByAriaLabel('New terminal').click();
    flushSync();
  }

  it('starts a terminal from the modal and focuses it for direct input', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'manual-1',
        command: 'codex',
        owner: null,
      }),
    });
    await openStartDialog();
    expect(document.querySelector('#terminal-start-arguments')).toBeNull();
    expect(
      document.querySelectorAll('#terminal-start-form .form-field__help'),
    ).toHaveLength(0);
    expect(
      document.querySelectorAll('#terminal-start-form .info-hint'),
    ).toHaveLength(4);

    setField('#terminal-start-command', 'codex --profile "work space"');
    setField('#terminal-start-workdir', 'C:\\repo');
    submitStartForm();

    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({
      command: 'codex',
      args: ['--profile', 'work space'],
      workdir: 'C:\\repo',
    });
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();
  });

  it('keeps malformed commands editable and starts the default shell when cleared', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({ owner: null }),
    });
    await openStartDialog();

    setField('#terminal-start-command', 'codex "unfinished');
    submitStartForm();
    await waitFor(() =>
      document.querySelector('#terminal-start-command[aria-invalid="true"]'),
    );
    expect(startTerminalMock).not.toHaveBeenCalled();
    await waitFor(() => document.activeElement.id === 'terminal-start-command');
    expect(document.querySelector('#terminal-start-command').value).toBe(
      'codex "unfinished',
    );

    setField('#terminal-start-command', '   ');
    flushSync();
    expect(document.querySelector('#terminal-start-command-error')).toBeNull();
    submitStartForm();
    await waitFor(() => startTerminalMock.mock.calls.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({});
  });

  it('prefills the last launch and can select an older persistent setup', async () => {
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
        launchHistory({
          id: 'older',
          command: 'python',
          args: ['-m', 'http.server', '8080'],
          workdir: 'C:\\Sites\\docs',
        }),
      ],
    });
    await openStartDialog();
    const field = (id) => document.querySelector(`#terminal-start-${id}`).value;
    expect(field('command')).toBe('codex --profile daily');
    expect(field('workdir')).toBe('C:\\Development\\vBot');

    document.querySelector('#terminal-start-history').click();
    const olderOption = () =>
      [...document.querySelectorAll('button')].find((button) =>
        button.textContent.includes('python -m http.server 8080'),
      );
    await waitFor(olderOption);
    olderOption().click();
    flushSync();
    expect(field('command')).toBe('python -m http.server 8080');
    expect(field('workdir')).toBe('C:\\Sites\\docs');
  });

  it.each([
    ['running', {}, true],
    ['finished', { state: 'exited', exit_code: 0 }, false],
  ])(
    'closes a %s Terminal Session with one click and removes its tile at once',
    async (_kind, changes, stops) => {
      await view.mountWith([terminal(changes)]);
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
    const finished = terminal({ group_id: 'finished', state: 'exited' });
    await view.mountWith(
      [finished],
      [
        manualGroup(),
        {
          group_id: 'finished',
          name: 'Finished',
          kind: 'finished',
          terminal_count: 1,
          live_count: 0,
          order: [],
        },
      ],
    );
    readyAll([finished], '\u001b[2Jretained final answer');

    expect(
      [...document.querySelectorAll('.terminals-view__group-tab')].some(
        (element) => element.textContent.includes('Finished'),
      ),
    ).toBe(true);
    expect(findButtonByAriaLabel('Close terminal')).toBeTruthy();
    expect(terminalInstances[0].options.disableStdin).toBe(true);
    expect(document.querySelector('.terminals-view__append-tile')).toBeNull();
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
            kind: 'output_settled',
            summary: 'Quiet is not a semantic prompt.',
          },
        }),
        shell,
      ],
      '\u001b[2JDemo TUI ready',
    );
    await flushAnimationFrames(2);

    const bars = document.querySelectorAll('.terminals-view__tile-bar');
    expect(bars).toHaveLength(2);
    expect(bars[0].textContent).toContain('Auth refactor · Codex');
    expect(bars[0].textContent).toContain('main@vbot');
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
    expect(terminalInstances[0].write).toHaveBeenCalledWith(
      '\u001b[2JDemo TUI ready',
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
      snapshots[0],
      expect.any(Function),
    );
    expect(terminalInstances[3].write).toHaveBeenCalledWith(
      snapshots[1],
      expect.any(Function),
    );
  });

  it('focuses the clicked tile, routes native keys to it, and exposes scrollback recovery', async () => {
    await view.mountWith(twoTerminals());
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
    expect(sendTerminalInputMock).toHaveBeenCalledWith('term-1', '\u001b[A\r');
    expect(sendTerminalInputMock).toHaveBeenCalledWith('term-2', 'ls');

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
    expect(sendTerminalInputMock).not.toHaveBeenCalled();
  });

  it('maximizes a tile, resizes its PTY at once, keeps it streaming, and restores the grid', async () => {
    await view.mountWith(twoTerminals());
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
    await waitFor(() => resizeTerminalMock.mock.calls.length > 0);
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 100, 32);

    streams[0].handlers.onEvent({
      type: 'terminal_output',
      sequence: 1,
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

  it('follows a one-shot tile shrink with a second fit so the PTY resizes', async () => {
    await view.mountWith([terminal()]);
    await wait(150);
    expect(resizeObservers).toHaveLength(1);
    resizeTerminalMock.mockClear();

    fixtureState.mockHostWidth = 400;
    resizeObservers[0].fire();
    await Promise.resolve();
    flushSync();
    expect(resizeTerminalMock).not.toHaveBeenCalled();

    await flushAnimationFrames(1);
    // A settled size is still debounced before it reaches the PTY.
    await wait(120);
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 50, 32);
  });

  it('re-fits the first tile when a second terminal joins the canvas', async () => {
    startTerminalMock.mockResolvedValue({
      terminal: terminal({ terminal_id: 'term-2', command: 'opencode' }),
    });
    await view.mountWith([terminal()]);
    const fitsBefore = fitAddons[0].fit.mock.calls.length;

    findButtonByAriaLabel('New terminal').click();
    flushSync();
    submitStartForm();
    await waitFor(() => terminalInstances.length === 2);
    await Promise.resolve();
    flushSync();
    await flushAnimationFrames(1);

    expect(fitAddons[0].fit.mock.calls.length).toBeGreaterThan(fitsBefore);
  });

  it('shows a diagnostics hint when a tile keeps rendering at a size the session never confirmed', async () => {
    await view.mountWith([terminal()]);
    // The tile fits 100x32 while the session still runs at its start size:
    // while the correction is in flight the hint stays quiet.
    await flushAnimationFrames(2);
    expect(document.querySelector('.terminals-view__grid-mismatch')).toBeNull();

    // The server confirms a different size than the fitted tile: the
    // pipeline closed without reconciling the two grids.
    resizeTerminalMock.mockResolvedValue({
      terminal: { columns: 120, rows: 40 },
    });
    fixtureState.mockHostHeight = 660;
    resizeObservers[0].fire();
    await flushAnimationFrames(2);
    await wait(150);
    await waitFor(() =>
      Boolean(document.querySelector('.terminals-view__grid-mismatch')),
    );
    expect(
      document.querySelector('.terminals-view__grid-mismatch').textContent,
    ).toContain('Session');
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
    expect(sendTerminalInputMock).not.toHaveBeenCalled();
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
    expect(sendTerminalInputMock).toHaveBeenCalledWith(
      'term-1',
      'Bitte prüfen und ergänzen.',
    );
    expect(microphone.getAttribute('aria-pressed')).toBe('false');
  });
});
