// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  listTerminalsMock,
  startTerminalMock,
  sendTerminalInputMock,
  resizeTerminalMock,
  killTerminalMock,
  setTerminalGroupOrderMock,
  streams,
  terminalInstances,
  fitAddons,
  resizeObservers,
  TerminalsView,
  terminal,
  terminalListResponse,
  flushAnimationFrames,
  waitFor,
  findButton,
  findButtonByAriaLabel,
  fixtureState,
  setupTerminalsViewSuite,
} from './TerminalsView.support.js';

describe('TerminalsView', () => {
  const suite = setupTerminalsViewSuite();

  it('shows active ownership and renders the live ANSI snapshot without owning lifetime', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 2,
      terminal: terminal({
        title: 'Auth refactor · Codex',
        columns: 100,
        rows: 28,
        attention: {
          revision: 1,
          kind: 'output_settled',
          summary: 'Quiet is not a semantic prompt.',
        },
      }),
      ansi: '\u001b[2JDemo TUI ready',
    });
    flushSync();

    expect(document.body.textContent).toContain('Auth refactor · Codex');
    expect(document.body.textContent).not.toContain('python');
    expect(document.body.textContent).not.toContain('PID 4321');
    expect(document.body.textContent).toContain('main@vbot');
    expect(document.querySelector('.terminals-view__tile-bar-meta')).toBeNull();
    expect(terminalInstances[0].options.theme.background).toBe('#0E0D0B');
    expect(terminalInstances[0].loadAddon).toHaveBeenCalledWith(fitAddons[0]);
    await flushAnimationFrames(2);
    expect(document.querySelector('button[role="switch"]')).toBeNull();
    expect(document.body.textContent).not.toContain(
      'Quiet is not a semantic prompt.',
    );
    expect(terminalInstances[0].resize).toHaveBeenCalled();
    expect(terminalInstances[0].write).toHaveBeenCalledWith(
      '\u001b[2JDemo TUI ready',
      expect.any(Function),
    );

    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;
    expect(streams[0].connection.close).toHaveBeenCalledWith(
      1000,
      'terminals-view-close',
    );
    expect(killTerminalMock).not.toHaveBeenCalled();
  });

  it('loads the FitAddon, fits the host grid, and never loads an accelerated renderer', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();

    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    await flushAnimationFrames(2);

    expect(fitAddons).toHaveLength(1);
    expect(terminalInstances[0].loadAddon).toHaveBeenCalledTimes(1);
    expect(terminalInstances[0].loadAddon).toHaveBeenCalledWith(fitAddons[0]);
    expect(fitAddons[0].fit).toHaveBeenCalled();
    expect(terminalInstances[0].cols).toBe(100);
    expect(terminalInstances[0].rows).toBe(32);
  });

  it('shows a diagnostics hint when a tile keeps rendering at a size the session never confirmed', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
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
    await new Promise((resolve) => setTimeout(resolve, 150));
    await waitFor(() =>
      Boolean(document.querySelector('.terminals-view__grid-mismatch')),
    );
    const hint = document.querySelector('.terminals-view__grid-mismatch');
    expect(hint.textContent).toContain('Session');
  });

  it('focuses on terminal click, forwards native keys, and exposes scrollback recovery', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    expect(terminalInstances[0].options.disableStdin).toBe(false);
    document.querySelector('.terminals-view__tile-host').dispatchEvent(
      new MouseEvent('pointerdown', {
        bubbles: true,
        button: 0,
      }),
    );
    flushSync();
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();

    terminalInstances[0].onDataCallback('\u001b[A');
    terminalInstances[0].onDataCallback('\u001b[I');
    terminalInstances[0].onDataCallback('\u001b[O');
    terminalInstances[0].onDataCallback('\r');
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(sendTerminalInputMock).toHaveBeenCalledWith('term-1', '\u001b[A\r');

    terminalInstances[0].buffer.active = { viewportY: 3, baseY: 12 };
    terminalInstances[0].onScrollCallback();
    flushSync();
    findButton('Jump to latest').click();
    expect(terminalInstances[0].scrollToBottom).toHaveBeenCalledTimes(1);
  });

  it('suppresses viewer protocol replies while preserving title and color changes', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => terminalInstances.length === 1);
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

  it('renders one tile per listed terminal with title, owner, and compact actions', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2);
    expect(streams).toHaveLength(2);
    await waitFor(() => terminalInstances.length === 2);
    expect(terminalInstances).toHaveLength(2);

    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles).toHaveLength(2);
    const firstBar = tiles[0].querySelector('.terminals-view__tile-bar');
    expect(firstBar.textContent).toContain('First terminal');
    expect(firstBar.textContent).toContain('main@vbot');
    expect(
      firstBar.querySelectorAll('.terminals-view__tile-action'),
    ).toHaveLength(3);
    expect(
      firstBar
        .querySelector('.terminals-view__tile-action svg')
        .getAttribute('width'),
    ).toBe('14');
    expect(
      document.querySelector('button[aria-label="Take control"]'),
    ).toBeNull();
    expect(
      document.querySelector('button[aria-label="Release control"]'),
    ).toBeNull();
  });

  it('maximizes a tile and resizes the PTY immediately from the fitted dimensions', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    await waitFor(() => fitAddons[0].fit.mock.calls.length > 0);

    expect(terminalInstances[0].cols).toBe(100);
    expect(terminalInstances[0].rows).toBe(32);
    // Maximize is a deterministic user action: its fit resizes the PTY
    // immediately, without waiting for a second measurement.
    document
      .querySelector('button[aria-label="Maximize"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await waitFor(() => resizeTerminalMock.mock.calls.length > 0);
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 100, 32);
  });

  it('follows a one-shot tile shrink with a second fit so the PTY resizes', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(resizeObservers).toHaveLength(1);
    resizeTerminalMock.mockClear();

    fixtureState.mockHostWidth = 400;
    resizeObservers[0].fire();
    await Promise.resolve();
    flushSync();
    expect(resizeTerminalMock).not.toHaveBeenCalled();

    await flushAnimationFrames(1);
    // A settled size is still debounced before it reaches the PTY.
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 50, 32);
  });

  it('re-fits the first tile when a second terminal joins the canvas', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'term-2',
        title: 'Second terminal',
        command: 'opencode',
      }),
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    const fitsBefore = fitAddons[0].fit.mock.calls.length;

    findButtonByAriaLabel('New terminal').click();
    flushSync();
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => terminalInstances.length === 2);
    await Promise.resolve();
    flushSync();
    await flushAnimationFrames(1);

    expect(fitAddons[0].fit.mock.calls.length).toBeGreaterThan(fitsBefore);
  });

  it('rebuilds both tiles after the Terminals tab is mounted again', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;

    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 4 && terminalInstances.length === 4);
    streams[2].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 2,
      terminal: terminal({
        terminal_id: 'term-1',
        title: 'First terminal',
        columns: 100,
        rows: 32,
      }),
      ansi: '\u001b[2Jfirst remount snapshot',
    });
    streams[3].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 2,
      terminal: terminal({
        terminal_id: 'term-2',
        title: 'Second terminal',
        columns: 100,
        rows: 32,
      }),
      ansi: '\u001b[2Jsecond remount snapshot',
    });
    flushSync();
    expect(terminalInstances[2].write).toHaveBeenCalledWith(
      '\u001b[2Jfirst remount snapshot',
      expect.any(Function),
    );
    expect(terminalInstances[3].write).toHaveBeenCalledWith(
      '\u001b[2Jsecond remount snapshot',
      expect.any(Function),
    );
  });

  it('focuses the first tile and switches focus via a tile bar click', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].classList.contains('terminals-view__tile--focused')).toBe(
      true,
    );
    expect(tiles[1].classList.contains('terminals-view__tile--focused')).toBe(
      false,
    );

    document
      .querySelectorAll('.terminals-view__tile-bar')[1]
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(
      document
        .querySelectorAll('.terminals-view__tile')[0]
        .classList.contains('terminals-view__tile--focused'),
    ).toBe(false);
    expect(
      document
        .querySelectorAll('.terminals-view__tile')[1]
        .classList.contains('terminals-view__tile--focused'),
    ).toBe(true);
    expect(terminalInstances[1].focus).toHaveBeenCalled();
  });

  it('maximizes a tile to fill the canvas and restores the grid', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    const canvas = document.querySelector('.terminals-view__canvas');
    expect(canvas.getAttribute('style')).toContain('repeat(2, minmax(0, 1fr))');

    document
      .querySelector('button[aria-label="Maximize"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(canvas.getAttribute('style')).toContain('repeat(1, minmax(0, 1fr))');
    expect(canvas.classList.contains('terminals-view__canvas--maximized')).toBe(
      true,
    );
    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].classList.contains('terminals-view__tile--maximized')).toBe(
      true,
    );
    expect(tiles[1].classList.contains('terminals-view__tile--hidden')).toBe(
      true,
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(resizeTerminalMock).toHaveBeenCalledWith('term-1', 100, 32);

    document
      .querySelector('button[aria-label="Restore"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(canvas.getAttribute('style')).toContain('repeat(2, minmax(0, 1fr))');
    expect(canvas.classList.contains('terminals-view__canvas--maximized')).toBe(
      false,
    );
    expect(
      document.querySelectorAll('.terminals-view__tile--hidden'),
    ).toHaveLength(0);
    expect(
      document.querySelectorAll('.terminals-view__tile--maximized'),
    ).toHaveLength(0);
  });

  it('activates the clicked tile and routes its input without ownership modes', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    document.querySelectorAll('.terminals-view__tile-host')[1].dispatchEvent(
      new MouseEvent('pointerdown', {
        bubbles: true,
        button: 0,
      }),
    );
    flushSync();

    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].classList.contains('terminals-view__tile--focused')).toBe(
      false,
    );
    expect(tiles[1].classList.contains('terminals-view__tile--focused')).toBe(
      true,
    );
    expect(terminalInstances[1].options.disableStdin).toBe(false);
    expect(terminalInstances[1].focus).toHaveBeenCalled();
    expect(terminalInstances[0].options.disableStdin).toBe(false);

    terminalInstances[1].onDataCallback('ls');
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(sendTerminalInputMock).toHaveBeenCalledWith('term-2', 'ls');
  });

  it('keeps maximized output streaming and restores without re-initializing tiles', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([
        terminal({ terminal_id: 'term-1', title: 'First terminal' }),
        terminal({ terminal_id: 'term-2', title: 'Second terminal' }),
      ]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    document
      .querySelector('button[aria-label="Maximize"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    streams[0].handlers.onEvent({
      type: 'terminal_output',
      sequence: 1,
      data: 'more output',
    });
    expect(terminalInstances[0].write).toHaveBeenCalledWith('more output');

    document
      .querySelector('button[aria-label="Restore"]')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    expect(terminalInstances).toHaveLength(2);
    expect(
      document.querySelectorAll('.terminals-view__tile-host'),
    ).toHaveLength(2);
  });

  it('reorders terminals in a user group by drag and drop on the tile bar', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [
          terminal({
            terminal_id: 'term-1',
            title: 'First terminal',
            group_id: 'group-1',
          }),
          terminal({
            terminal_id: 'term-2',
            title: 'Second terminal',
            group_id: 'group-1',
          }),
        ],
        [
          {
            group_id: 'group-1',
            name: 'Work',
            kind: 'user',
            terminal_count: 2,
            live_count: 2,
            order: [],
          },
        ],
      ),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);

    const bars = document.querySelectorAll('.terminals-view__tile-bar');
    expect(bars[0].getAttribute('draggable')).toBe('true');

    function dragEvent(type) {
      const event = new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
      });
      let payload = '';
      const dataTransfer = {
        effectAllowed: '',
        dropEffect: '',
        setData: (_kind, value) => {
          payload = value;
        },
        getData: () => payload,
      };
      Object.defineProperty(event, 'dataTransfer', { value: dataTransfer });
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
    const tiles = document.querySelectorAll('.terminals-view__tile');
    expect(tiles[0].getAttribute('data-terminal-id')).toBe('term-2');
  });
});
