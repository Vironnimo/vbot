// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  flushSync,
  mount,
  unmount,
  listTerminalsMock,
  startTerminalMock,
  sendTerminalInputMock,
  killTerminalMock,
  subscribeTerminalEventsMock,
  createAudioRecorderMock,
  transcribeSpeechMock,
  streams,
  terminalInstances,
  TerminalsView,
  terminal,
  manualGroup,
  terminalListResponse,
  waitFor,
  findButton,
  findButtonByAriaLabel,
  setupTerminalsViewSuite,
} from './TerminalsView.support.js';

describe('TerminalsView', () => {
  const suite = setupTerminalsViewSuite();

  it('lets voice select, maximize and restore the real Terminal layout', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([terminal(), terminal({ terminal_id: 'term-2' })]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    await waitFor(() => terminalInstances.length === 2);
    await suite.mountedComponent.applyVoiceAction('maximize', {
      terminal_id: 'term-2',
    });
    flushSync();
    expect(suite.mountedComponent.getVoiceContext().maximized_terminal_id).toBe(
      'term-2',
    );
    expect(suite.mountedComponent.getVoiceContext().selected_terminal_id).toBe(
      'term-2',
    );
    await suite.mountedComponent.applyVoiceAction('restore');
    flushSync();
    expect(suite.mountedComponent.getVoiceContext().maximized_terminal_id).toBe(
      '',
    );
    expect(suite.mountedComponent.getVoiceContext().visible_order).toEqual([
      'term-1',
      'term-2',
    ]);
    expect(killTerminalMock).not.toHaveBeenCalled();
    expect(sendTerminalInputMock).not.toHaveBeenCalled();
  });

  it('lets voice select an empty group and clears a previous maximize', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal()],
        [
          manualGroup({ terminal_count: 1, live_count: 1 }),
          {
            group_id: 'review',
            name: 'Review',
            kind: 'user',
            terminal_count: 0,
            live_count: 0,
            order: [],
          },
        ],
      ),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    await waitFor(() => terminalInstances.length === 1);
    await suite.mountedComponent.applyVoiceAction('maximize', {
      terminal_id: 'term-1',
    });
    await suite.mountedComponent.applyVoiceAction('show_group', {
      group_id: 'review',
    });
    flushSync();
    expect(suite.mountedComponent.getVoiceContext()).toMatchObject({
      selected_group_id: 'review',
      maximized_terminal_id: '',
      visible_order: [],
    });
    expect(document.querySelector('.terminal-tile')).toBeNull();
    await expect(
      suite.mountedComponent.applyVoiceAction('show_group', {
        group_id: 'missing',
      }),
    ).rejects.toThrow('group_not_found');
    expect(suite.mountedComponent.getVoiceContext().selected_group_id).toBe(
      'review',
    );
    expect(killTerminalMock).not.toHaveBeenCalled();
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
    listTerminalsMock.mockResolvedValue(
      terminalListResponse([terminal(), terminal({ terminal_id: 'term-2' })]),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    await waitFor(() => terminalInstances.length === 2);
    streams.forEach((stream, index) =>
      stream.handlers.onEvent({
        type: 'terminal_ready',
        sequence: 1,
        ansi: '',
        terminal: terminal({ terminal_id: `term-${index + 1}` }),
      }),
    );
    flushSync();
    const microphone = document.querySelector(
      '[data-terminal-id="term-1"] button[aria-label="Dictate into terminal"]',
    );
    microphone.click();
    await waitFor(() => microphone.getAttribute('aria-pressed') === 'true');
    expect(recorder.start).toHaveBeenCalledOnce();
    expect(
      document.querySelector(
        '[data-terminal-id="term-2"] button[aria-label="Dictate into terminal"]',
      ).disabled,
    ).toBe(true);
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
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(sendTerminalInputMock).toHaveBeenCalledWith(
      'term-1',
      'Bitte prüfen und ergänzen.',
    );
    expect(microphone.getAttribute('aria-pressed')).toBe('false');
  });

  it('discards an in-flight transcript when the view is unmounted', async () => {
    const recorder = {
      start: vi.fn(),
      cancel: vi.fn(),
      stop: vi.fn().mockResolvedValue(new Blob(['audio'])),
      filename: () => 'recording.webm',
    };
    createAudioRecorderMock.mockResolvedValue(recorder);
    let finish;
    transcribeSpeechMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    listTerminalsMock.mockResolvedValue(terminalListResponse([terminal()]));
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    await waitFor(() => terminalInstances.length === 1);
    streams[0].handlers.onEvent({
      type: 'terminal_ready',
      sequence: 1,
      ansi: '',
      terminal: terminal(),
    });
    flushSync();
    findButtonByAriaLabel('Dictate into terminal').click();
    await waitFor(() => recorder.start.mock.calls.length === 1);
    findButtonByAriaLabel('Stop recording and insert text').click();
    await waitFor(() => transcribeSpeechMock.mock.calls.length === 1);
    await unmount(suite.mountedComponent);
    suite.mountedComponent = null;
    expect(transcribeSpeechMock.mock.calls[0][1].signal.aborted).toBe(true);
    finish({ text: 'must not arrive' });
    await Promise.resolve();
    expect(terminalInstances[0].paste).not.toHaveBeenCalled();
  });

  it('uses the compact top group bar and keeps both terminal actions available', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [manualGroup({ terminal_count: 1, live_count: 1 })],
      terminals: [terminal()],
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

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

  it.each([1, 2, 3, 4, 5, 6, 7, 8, 9])(
    'places the narrow append tile after %i terminals without another row',
    async (count) => {
      listTerminalsMock.mockResolvedValue(
        terminalListResponse(
          Array.from({ length: count }, (_, index) =>
            terminal({ terminal_id: `term-${index}` }),
          ),
        ),
      );
      suite.mountedComponent = mount(TerminalsView, { target: document.body });
      await waitFor(() => terminalInstances.length === count);
      const canvas = document.querySelector('.terminals-view__canvas');
      const tiles = [...canvas.querySelectorAll('.terminals-view__tile')];
      const last = tiles.at(-1);
      const append = canvas.querySelector('.terminals-view__append-tile');
      expect(canvas.lastElementChild).toBe(append);
      expect(append.style.gridRow).toBe(last.style.gridRow.split(' / ')[0]);
      expect(canvas.style.gridTemplateRows).toBe(
        `repeat(${count <= 2 ? 1 : Math.ceil(count / (count <= 4 ? 2 : count <= 6 ? 3 : 4))}, minmax(0, 1fr))`,
      );
      expect(
        tiles
          .slice(0, -1)
          .some((tile) =>
            tile.classList.contains('terminals-view__tile--append-space'),
          ),
      ).toBe(false);
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
    const group = {
      group_id: 'work',
      name: 'Work',
      kind: 'user',
      terminal_count: empty ? 0 : 1,
      live_count: empty ? 0 : 1,
      order: [],
    };
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(empty ? [] : [terminal({ group_id: 'work' })], [
        group,
      ]),
    );
    startTerminalMock.mockResolvedValue({
      terminal: terminal({ terminal_id: 'new-work', group_id: 'work' }),
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    await waitFor(() => document.querySelector(selector));
    document.querySelector(selector).click();
    flushSync();
    document
      .querySelector('#terminal-start-form')
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => startTerminalMock.mock.calls.length === 1);
    expect(startTerminalMock).toHaveBeenCalledWith({ group_id: 'work' });
    await waitFor(() =>
      document.querySelector('[data-terminal-id="new-work"]'),
    );
    const append = document.querySelector('.terminals-view__append-tile');
    expect(append.previousElementSibling.dataset.terminalId).toBe('new-work');
  });

  it('disables both creation locations when the server is unavailable', async () => {
    suite.mountedComponent = mount(TerminalsView, {
      target: document.body,
      props: { serverUnavailable: true },
    });
    flushSync();
    expect(findButtonByAriaLabel('Add group').disabled).toBe(true);
    expect(findButton('New terminal').disabled).toBe(true);
  });

  it('switches the visible terminal group from the top group bar', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal()],
        [
          manualGroup({ terminal_count: 1, live_count: 1 }),
          {
            group_id: 'group-work',
            name: 'Work',
            kind: 'user',
            terminal_count: 0,
            live_count: 0,
            order: [],
          },
        ],
      ),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    [...document.querySelectorAll('.terminals-view__group-tab')]
      .find((button) => button.textContent.includes('Work'))
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();

    expect(
      [...document.querySelectorAll('.terminals-view__group-tab')]
        .find((button) => button.textContent.includes('Work'))
        .getAttribute('aria-current'),
    ).toBe('true');
    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
  });

  it('reveals a single group action menu instead of two inline buttons', async () => {
    listTerminalsMock.mockResolvedValue(
      terminalListResponse(
        [terminal({ group_id: 'group-work' })],
        [
          {
            group_id: 'group-work',
            name: 'Work',
            kind: 'user',
            terminal_count: 1,
            live_count: 1,
            order: [],
          },
        ],
      ),
    );
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    const trigger = document.querySelector(
      '.terminals-view__group-action-menu-trigger',
    );
    expect(trigger).toBeTruthy();
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu');

    // The menu is closed until the trigger is engaged.
    expect(document.querySelector('.terminals-view__group-menu')).toBeNull();

    trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    flushSync();
    await waitFor(() => document.querySelector('.terminals-view__group-menu'));

    const items = [
      ...document.querySelectorAll('.terminals-view__group-menu-item'),
    ];
    expect(
      items.some((item) => item.textContent.trim() === 'Rename group'),
    ).toBe(true);
    expect(
      items.some((item) => item.textContent.trim() === 'Delete group'),
    ).toBe(true);
    const deleteItem = items.find(
      (item) => item.textContent.trim() === 'Delete group',
    );
    expect(deleteItem.classList).toContain(
      'terminals-view__group-menu-item--danger',
    );
  });

  it('renders the shared empty state when no Terminal Session is active', async () => {
    listTerminalsMock.mockResolvedValue({ groups: [], terminals: [] });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() =>
      document.querySelector('.terminals-view__group-status'),
    );

    expect(
      document.querySelector('.terminals-view__detail > .empty-state'),
    ).toBeTruthy();
    expect(subscribeTerminalEventsMock).not.toHaveBeenCalled();
  });

  it('humanizes a shell command while no program has announced a title', async () => {
    listTerminalsMock.mockResolvedValue({
      groups: [manualGroup({ terminal_count: 1, live_count: 1 })],
      terminals: [terminal({ command: 'pwsh.exe', title: '' })],
    });
    suite.mountedComponent = mount(TerminalsView, { target: document.body });
    flushSync();
    await waitFor(() => streams.length === 1);

    expect(document.body.textContent).toContain('PowerShell');
    expect(document.body.textContent).not.toContain('pwsh.exe');
  });
});
