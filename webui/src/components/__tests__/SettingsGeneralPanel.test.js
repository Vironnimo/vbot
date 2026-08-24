// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { reactiveProps } from './_reactiveProps.svelte.js';

const listClientsMock = vi.fn();
const updateSettingsMock = vi.fn();
const resolveClientConnectionIdMock = vi.fn(() => 'tab-self');

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => ({
  listClients: (...args) => listClientsMock(...args),
  updateSettings: (...args) => updateSettingsMock(...args),
}));

vi.mock('$lib/clientIdentity.js', () => ({
  resolveClientConnectionId: (...args) =>
    resolveClientConnectionIdMock(...args),
}));

const { default: SettingsGeneralPanel } =
  await import('../settings/SettingsGeneralPanel.svelte');

function roster() {
  return {
    clients: [
      {
        id: 'reg-1',
        connection_id: 'tab-self',
        accessor: 'browser',
        browser: 'Chrome',
        os: 'Windows',
        connected_at: '2026-06-20T10:00:00+00:00',
        status: 'connected',
      },
      {
        id: 'reg-2',
        connection_id: 'tab-other',
        accessor: 'desktop',
        browser: 'Unknown',
        os: 'Linux',
        connected_at: '2026-06-20T11:00:00+00:00',
        status: 'connected',
      },
    ],
  };
}

async function flushAsync() {
  await Promise.resolve();
  await Promise.resolve();
  flushSync();
}

describe('SettingsGeneralPanel', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    listClientsMock.mockReset();
    updateSettingsMock.mockReset();
    resolveClientConnectionIdMock.mockReset();
    resolveClientConnectionIdMock.mockReturnValue('tab-self');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  it('renders the connected-clients roster and marks the own window', async () => {
    listClientsMock.mockResolvedValue(roster());

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: { settings: null, clientsRefreshToken: 0 },
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain('Chrome');

    expect(document.body.querySelectorAll('.s-client-row')).toHaveLength(2);
    expect(document.body.querySelectorAll('.s-client-row--own')).toHaveLength(
      1,
    );

    const ownRow = document.body.querySelector('.s-client-row--own');
    expect(ownRow.textContent).toContain('Chrome');
  });

  it('shows the empty state when no windows are connected', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: { settings: null, clientsRefreshToken: 0 },
    });
    flushSync();
    await flushAsync();

    expect(document.body.querySelector('.empty-state')).toBeTruthy();
    expect(document.body.querySelectorAll('.s-client-row')).toHaveLength(0);
  });

  it('reloads the roster when clientsRefreshToken changes', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    const props = reactiveProps({ settings: null, clientsRefreshToken: 0 });

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await flushAsync();

    const callsBefore = listClientsMock.mock.calls.length;
    expect(callsBefore).toBeGreaterThanOrEqual(1);

    props.clientsRefreshToken = 1;
    flushSync();
    await flushAsync();

    expect(listClientsMock.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it('shows an error message when the roster fails to load', async () => {
    listClientsMock.mockRejectedValue(new Error('boom'));

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: { settings: null, clientsRefreshToken: 0 },
    });
    flushSync();
    await flushAsync();

    expect(document.body.querySelector('.banner--error')).toBeTruthy();
  });

  it('opens the setup guide when the re-entry button is clicked', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    const onOpenSetupGuide = vi.fn();

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: { settings: null, clientsRefreshToken: 0, onOpenSetupGuide },
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain('Setup guide');

    const setupButton = Array.from(
      document.body.querySelectorAll('button'),
    ).find((button) => button.textContent.includes('Open setup guide'));
    expect(setupButton).toBeTruthy();

    setupButton.click();
    flushSync();

    expect(onOpenSetupGuide).toHaveBeenCalledTimes(1);
  });

  it('renders the keep-awake toggle from the settings state', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    const props = reactiveProps({
      settings: { general: { keep_awake: true } },
      clientsRefreshToken: 0,
    });

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await flushAsync();

    const toggle = document.body.querySelector('[role="switch"]');
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe('Keep computer awake');

    props.settings = { general: { keep_awake: false } };
    flushSync();
    expect(
      document.body
        .querySelector('[role="switch"]')
        .getAttribute('aria-checked'),
    ).toBe('false');
  });

  it('saves the keep-awake setting when the toggle changes', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    updateSettingsMock.mockResolvedValue({
      general: { keep_awake: true },
    });
    const onCommit = vi.fn();
    const onToast = vi.fn();

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: {
        settings: { general: { keep_awake: false } },
        clientsRefreshToken: 0,
        onCommit,
        onToast,
      },
    });
    flushSync();
    await flushAsync();

    document.body.querySelector('[role="switch"]').click();
    await flushAsync();

    expect(updateSettingsMock).toHaveBeenCalledWith({
      server: { keep_awake: true },
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' }),
    );
  });

  it('shows an error banner and keeps the old value when saving fails', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    updateSettingsMock.mockRejectedValue(new Error('boom'));
    const onError = vi.fn();

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: {
        settings: { general: { keep_awake: false } },
        clientsRefreshToken: 0,
        onError,
      },
    });
    flushSync();
    await flushAsync();

    document.body.querySelector('[role="switch"]').click();
    await flushAsync();

    expect(onError).toHaveBeenCalledWith(expect.stringContaining('boom'));
    expect(
      document.body
        .querySelector('[role="switch"]')
        .getAttribute('aria-checked'),
    ).toBe('false');
  });
});
