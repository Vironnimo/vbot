// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { mount, flushSync, unmount } = await import('svelte');
const { default: TerminalLauncher } =
  await import('../terminals/TerminalLauncher.svelte');
const { default: ComputerContextHost } =
  await import('./ComputerContextHost.support.svelte');

let component;
let catalog;
let listed;
let start;
const picker = () => document.querySelector('button[aria-label="Computer"]');
const startButton = () => document.querySelector('.terminal-launcher__start');
const history = () =>
  document.querySelector('.terminal-launcher__setups')?.textContent ?? '';

async function chooseComputer(label) {
  picker().click();
  flushSync();
  await vi.waitFor(() =>
    expect(document.querySelector('[role="option"]')).not.toBeNull(),
  );
  [...document.querySelectorAll('.s-dropdown-panel [role="option"]')]
    .find((item) => item.textContent.includes(label))
    .click();
  flushSync();
}

beforeEach(() => {
  document.body.innerHTML = '<button id="anchor">Open</button>';
  listed = [
    { id: 'server', name: 'Server', online: true, capabilities: ['terminal'] },
    {
      id: 'pc_laptop',
      name: 'Work laptop',
      online: true,
      capabilities: ['terminal'],
    },
    {
      id: 'pc_offline',
      name: 'Office PC',
      online: false,
      capabilities: ['terminal'],
    },
    {
      id: 'pc_screen',
      name: 'Screen only',
      online: true,
      capabilities: ['computer_use'],
    },
  ];
  start = vi.fn(async () => ({ terminal_id: 'started' }));
  component = mount(ComputerContextHost, {
    target: document.body,
    props: {
      component: TerminalLauncher,
      readComputers: async () => ({ computers: listed }),
      onCatalog: (value) => {
        catalog = value;
      },
      componentProps: {
        anchor: document.getElementById('anchor'),
        controller: { startManualTerminal: start },
        viewState: {
          groups: [],
          selectedGroupId: '',
          startingTerminal: false,
          startError: '',
          launchHistory: [
            {
              id: 'remote',
              computer_id: 'pc_laptop',
              command: 'codex',
              args: ['--profile', 'work'],
              workdir: 'C:\\work\\repo',
            },
            { id: 'local', command: 'bash', args: [], workdir: '/srv/project' },
          ],
        },
      },
    },
  });
  flushSync();
});

afterEach(async () => {
  await unmount(component);
  vi.restoreAllMocks();
});

describe('Terminal launcher computer choice', () => {
  it('keeps recent commands and directories on their computer and sends the explicit remote target', async () => {
    await vi.waitFor(() => expect(catalog.loaded).toBe(true));
    expect(history()).toContain('bash');
    expect(history()).not.toContain('codex');
    await chooseComputer('Work laptop');
    expect(history()).toContain('codex');
    expect(history()).not.toContain('bash');

    document.querySelector('.terminal-launcher__chip--folder').click();
    flushSync();
    const cwd = document.querySelector(
      'input[aria-label="Working directory on the selected computer"]',
    );
    expect(cwd.value).toBe('C:\\work\\repo');
    expect(document.querySelector('.path-field')).toBeNull();
    cwd.value = 'D:\\other';
    cwd.dispatchEvent(new Event('input', { bubbles: true }));
    cwd.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    flushSync();
    const command = document.querySelector('.terminal-launcher__input');
    command.value = 'claude';
    command.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    startButton().click();
    await vi.waitFor(() => expect(start).toHaveBeenCalledOnce());
    expect(start).toHaveBeenCalledWith(
      { command: 'claude', workdir: 'D:\\other', computer_id: 'pc_laptop' },
      { groupId: null },
    );
  });

  it('keeps offline and missing targets selected and prevents starting without the shared Terminal capability', async () => {
    await vi.waitFor(() => expect(catalog.loaded).toBe(true));
    await chooseComputer('Office PC');
    expect(picker().textContent).toContain('Office PC');
    expect(startButton().disabled).toBe(true);
    startButton().click();
    expect(start).not.toHaveBeenCalled();

    listed = listed.filter((computer) => computer.id !== 'pc_offline');
    await catalog.refresh();
    flushSync();
    expect(picker().textContent).not.toContain('Server');
    expect(startButton().disabled).toBe(true);
    picker().click();
    flushSync();
    const unavailable = [
      ...document.querySelectorAll('.s-dropdown-panel [role="option"]'),
    ].find((item) => item.textContent.includes('Screen only'));
    expect(unavailable.disabled).toBe(true);
    expect(unavailable.textContent).toContain('Terminal unavailable');
    expect(start).not.toHaveBeenCalled();
  });
});
