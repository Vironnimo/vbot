// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  listTerminalsMock,
  startTerminalMock,
  killTerminalMock,
  forgetTerminalMock,
  streams,
  terminalInstances,
  TerminalsView,
  terminal,
  launchHistory,
  manualGroup,
  terminalListResponse,
  waitFor,
  findButtonByAriaLabel,
  setField,
  setupTerminalsViewSuite,
} from './TerminalsView.support.js';

describe('TerminalsView', () => {
  const suite = setupTerminalsViewSuite();

  it('rebuilds retained scrollback when the Terminals tab is mounted again', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 1,
      terminal: terminal(),
      ansi: '\u001b[2Jfirst visit',
    });
    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;

    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 2 && terminalInstances.length === 2);
    const retainedSnapshot =
      '\u001b[2J\u001b[Hhistory-0\r\nhistory-1\r\ncurrent screen';
    streams[1].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 4,
      terminal: terminal(),
      ansi: retainedSnapshot,
    });

    expect(terminalInstances[1].write).toHaveBeenCalledWith(
      retainedSnapshot,
      expect.any(Function),
    );
  });

  it('keeps a finished Terminal Session available as read-only history', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [
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
      terminals: [terminal({ group_id: 'finished', state: 'exited' })],
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 6,
      terminal: terminal({ group_id: 'finished', state: 'exited' }),
      ansi: '\u001b[2Jretained final answer',
    });
    flushSync();

    expect(
      [...document.querySelectorAll('.terminals-view__group-tab')].some(
        (element) => element.textContent.includes('Finished'),
      ),
    ).toBe(true);
    expect(
      [...document.querySelectorAll('button')].some(
        (button) => button.getAttribute('aria-label') === 'Close terminal',
      ),
    ).toBe(true);
    expect(terminalInstances[0].options.disableStdin).toBe(true);
  });

  it('closes a finished Terminal Session and removes it from the list', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([terminal({ state: 'exited', exit_code: 0 })]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 6,
      terminal: terminal({ state: 'exited', exit_code: 0 }),
      ansi: '\\u001b[2Jretained final answer',
    });
    flushSync();

    expect(document.querySelector('.terminals-view__exit-code')).toBeNull();

    findButtonByAriaLabel('Close terminal').click();
    flushSync();
    // The tile disappears immediately; the catalog removal runs in the
    // background.
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(0);
    await waitFor(() => forgetTerminalMock.mock.calls.length > 0);
    expect(forgetTerminalMock).toHaveBeenCalledWith('term-1');
    expect(killTerminalMock).not.toHaveBeenCalled();
  });

  it('closes a running Terminal Session with one click: stop, then remove', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);

    findButtonByAriaLabel('Close terminal').click();
    flushSync();
    // The tile disappears immediately; the server-side stop and catalog
    // removal continue in the background — no confirmation dialog in
    // between.
    expect(document.querySelectorAll('.terminals-view__tile')).toHaveLength(0);
    await waitFor(() => killTerminalMock.mock.calls.length > 0);
    await waitFor(() => forgetTerminalMock.mock.calls.length > 0);
    expect(killTerminalMock).toHaveBeenCalledWith('term-1');
    expect(forgetTerminalMock).toHaveBeenCalledWith('term-1');
    expect(
      document.querySelector('[role="dialog"][aria-modal="true"]'),
    ).toBeNull();
  });

  it('starts a terminal from the modal and focuses it for direct input', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({
        terminal_id: 'manual-1',
        command: 'codex',
        owner: null,
        group_id: 'auto:manual',
      }),
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );

    findButtonByAriaLabel('New terminal').click();
    flushSync();
    expect(document.querySelector('#terminal-start-arguments')).toBeNull();
    expect(
      document.querySelectorAll('#terminal-start-form .form-field__help'),
    ).toHaveLength(0);
    expect(
      document.querySelectorAll('#terminal-start-form .info-hint'),
    ).toHaveLength(4);
    setField('#terminal-start-command', 'codex --profile "work space"');
    setField('#terminal-start-workdir', 'C:\\repo');
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));

    await waitFor(() => streams.length === 1 && terminalInstances.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({
      command: 'codex',
      args: ['--profile', 'work space'],
      workdir: 'C:\\repo',
    });
    expect(terminalInstances[0].options.disableStdin).toBe(false);
    expect(terminalInstances[0].focus).toHaveBeenCalled();
    expect(
      document.querySelector('button[aria-label="Take control"]'),
    ).toBeNull();
    expect(
      document.querySelector('button[aria-label="Release control"]'),
    ).toBeNull();
  });

  it('keeps malformed commands editable and starts the default shell when cleared', async () => {
    listTerminalsMock.mockResolvedValue(terminalListResponse([]));
    startTerminalMock.mockResolvedValue({
      terminal: terminal({ owner: null }),
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );
    findButtonByAriaLabel('New terminal').click();
    flushSync();
    setField('#terminal-start-command', 'codex "unfinished');
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
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
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
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
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__detail > .empty-state'),
    );

    findButtonByAriaLabel('New terminal').click();
    flushSync();
    expect(document.querySelector('#terminal-start-command').value).toBe(
      'codex --profile daily',
    );
    expect(document.querySelector('#terminal-start-workdir').value).toBe(
      'C:\\Development\\vBot',
    );

    document.querySelector('#terminal-start-history').click();
    await waitFor(() =>
      [...document.querySelectorAll('button')].some((button) =>
        button.textContent.includes('python -m http.server 8080'),
      ),
    );
    [...document.querySelectorAll('button')]
      .find((button) =>
        button.textContent.includes('python -m http.server 8080'),
      )
      .click();
    flushSync();
    expect(document.querySelector('#terminal-start-command').value).toBe(
      'python -m http.server 8080',
    );
    expect(document.querySelector('#terminal-start-workdir').value).toBe(
      'C:\\Sites\\docs',
    );
  });
});
