// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const { mount, flushSync, unmount } = await import('svelte');
const { createAutosaveCoordinator } = await import('../../lib/autosave.js');
const { default: DesktopComputerSharing } =
  await import('../settings/DesktopComputerSharing.svelte');
const { default: AutosaveHost } =
  await import('./AutosaveContextHost.support.svelte');

let component;
let coordinator;
let status;
let getStatus;
let setStatus;
const toggle = (label) =>
  document.querySelector(`button[aria-label="${label}"]`);
const name = () => document.querySelector('input');

beforeEach(() => {
  document.body.innerHTML = '';
  status = {
    computer_id: null,
    name: 'Laptop',
    server_url: 'https://vbot.example',
    connected: true,
    capabilities: { terminal: false, computer_use: false },
    available: { terminal: true, computer_use: false },
    active_terminals: 0,
  };
  getStatus = vi.fn(async () => status);
  setStatus = vi.fn(async (changes) => {
    status = {
      ...status,
      computer_id:
        changes.terminal || changes.computer_use
          ? 'pc_laptop'
          : status.computer_id,
      name: changes.name ?? status.name,
      capabilities: {
        ...status.capabilities,
        ...(changes.terminal === undefined
          ? {}
          : { terminal: changes.terminal }),
        ...(changes.computer_use === undefined
          ? {}
          : { computer_use: changes.computer_use }),
      },
    };
    return status;
  });
  window.pywebview = {
    api: {
      getComputerSharing: getStatus,
      setComputerSharing: (...args) => setStatus(...args),
    },
  };
  coordinator = createAutosaveCoordinator();
  component = mount(AutosaveHost, {
    target: document.body,
    props: { component: DesktopComputerSharing, coordinator },
  });
  flushSync();
});

afterEach(async () => {
  await unmount(component);
  delete window.pywebview;
  vi.restoreAllMocks();
});

describe('Desktop computer sharing', () => {
  it('grants sharing on a fresh installation before a computer ID exists and keeps unavailable capabilities disabled', async () => {
    await vi.waitFor(() => expect(toggle('Share Terminal')).not.toBeNull());
    window.dispatchEvent(
      new CustomEvent('vbot-desktop-computer-sharing', {
        detail: { ...status, connected: false },
      }),
    );
    flushSync();
    expect(document.querySelector('[role="status"]').textContent).toContain(
      'Not connected',
    );
    expect(toggle('Share Computer Use').disabled).toBe(true);
    toggle('Share Terminal').click();
    flushSync();
    await coordinator.flushPending();
    expect(setStatus).toHaveBeenCalledWith({ terminal: true });
    expect(status.computer_id).toBe('pc_laptop');
    expect(toggle('Share Terminal').getAttribute('aria-checked')).toBe('true');
    expect(coordinator.hasPending()).toBe(false);

    setStatus.mockRejectedValueOnce(new Error('declined'));
    toggle('Share Terminal').click();
    flushSync();
    await coordinator.flushPending();
    flushSync();
    expect(toggle('Share Terminal').getAttribute('aria-checked')).toBe('true');
    expect(coordinator.hasPending()).toBe(false);
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it('preserves a newer name while an earlier save is running and flushes both in order', async () => {
    await vi.waitFor(() => expect(name()).not.toBeNull());
    let resolve;
    setStatus.mockImplementationOnce(
      (changes) =>
        new Promise((done) => {
          resolve = () => done({ ...status, name: changes.name });
        }),
    );
    name().value = 'Office';
    name().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const saving = coordinator.flushPending();
    await vi.waitFor(() => expect(resolve).toBeTypeOf('function'));
    name().value = 'Office laptop';
    name().dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    resolve();
    await saving;
    flushSync();
    expect(setStatus.mock.calls.map(([change]) => change)).toEqual([
      { name: 'Office' },
      { name: 'Office laptop' },
    ]);
    expect(name().value).toBe('Office laptop');
    expect(coordinator.hasPending()).toBe(false);
  });
});
