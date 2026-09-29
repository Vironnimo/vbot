// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

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
      {
        id: 'reg-3',
        connection_id: 'tray-1',
        accessor: 'tray',
        browser: 'Unknown',
        os: 'Windows',
        connected_at: '2026-06-20T12:00:00+00:00',
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

  it('renders the connected-clients roster with accessor labels and marks the own window', async () => {
    listClientsMock.mockResolvedValue(roster());

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: { settings: null, clientsRefreshToken: 0 },
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain('Chrome');

    const rows = document.body.querySelectorAll('.s-client-row');
    expect(
      Array.from(rows, (row) => row.querySelector('.s-row-label').textContent),
    ).toEqual([
      t('settings.general.clients.accessor.browser'),
      t('settings.general.clients.accessor.desktop'),
      t('settings.general.clients.accessor.tray'),
    ]);
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
      props: {
        page: 'preferences',
        settings: null,
        clientsRefreshToken: 0,
        onOpenSetupGuide,
      },
    });
    flushSync();
    await flushAsync();

    const setupButton = document.body.querySelector(
      `button[aria-label="${t('settings.general.setupGuideAction')}"]`,
    );
    expect(setupButton).toBeTruthy();

    setupButton.click();
    flushSync();

    expect(onOpenSetupGuide).toHaveBeenCalledTimes(1);
  });

  it('shows the server facts and copies the version and data directory', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: {
        settings: {
          general: {
            server: { listen_host: '127.0.0.1', listen_port: 8420 },
            data_directory: 'C:/data',
            build: {
              version: '0.4.4',
              revision: 'adf68bf91d57e12362f63ef0e8728849eb9e4cb7',
              branch: 'main',
              release: false,
            },
          },
        },
        clientsRefreshToken: 0,
      },
    });
    flushSync();
    await flushAsync();

    expect(document.body.textContent).toContain('127.0.0.1:8420');
    expect(document.body.textContent).toContain('0.4.4 · main · adf68bf9');
    document.body
      .querySelector(
        `button[aria-label="${t('settings.general.copyVersion')}"]`,
      )
      .click();
    await flushAsync();
    expect(writeText).toHaveBeenCalledWith(
      '0.4.4 · main · adf68bf91d57e12362f63ef0e8728849eb9e4cb7',
    );
    document.body
      .querySelector(
        `button[aria-label="${t('settings.general.copyDataDirectory')}"]`,
      )
      .click();
    await flushAsync();
    expect(writeText).toHaveBeenCalledWith('C:/data');
    delete navigator.clipboard;
  });

  it('renders the keep-awake toggle from settings and saves a change', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    updateSettingsMock.mockResolvedValue({
      general: { keep_awake: false },
    });
    const onCommit = vi.fn();
    const onToast = vi.fn();

    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props: {
        settings: { general: { keep_awake: true } },
        clientsRefreshToken: 0,
        onCommit,
        onToast,
      },
    });
    flushSync();
    await flushAsync();

    const toggle = document.body.querySelector('[role="switch"]');
    expect(toggle.getAttribute('aria-checked')).toBe('true');
    expect(toggle.getAttribute('aria-label')).toBe('Keep computer awake');

    toggle.click();
    await flushAsync();

    expect(updateSettingsMock).toHaveBeenCalledWith({
      server: { keep_awake: false },
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onToast).not.toHaveBeenCalled();
  });

  it('shows the save error and preserves the pending choice for retry', async () => {
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
    ).toBe('true');
  });

  it('saves a previously saved choice again after an external settings change', async () => {
    listClientsMock.mockResolvedValue({ clients: [] });
    updateSettingsMock.mockResolvedValue({ general: { keep_awake: true } });
    const props = reactiveProps({
      settings: { general: { keep_awake: false } },
      onCommit: (settings) => {
        props.settings = settings;
      },
    });
    mountedComponent = mount(SettingsGeneralPanel, {
      target: document.body,
      props,
    });
    flushSync();
    await flushAsync();
    document.body.querySelector('[role="switch"]').click();
    await flushAsync();
    await flushAsync();
    expect(updateSettingsMock).toHaveBeenCalledOnce();

    props.settings = { general: { keep_awake: false } };
    flushSync();
    expect(
      document.body
        .querySelector('[role="switch"]')
        .getAttribute('aria-checked'),
    ).toBe('false');
    document.body.querySelector('[role="switch"]').click();
    await flushAsync();

    expect(updateSettingsMock).toHaveBeenCalledTimes(2);
    expect(props.settings.general.keep_awake).toBe(true);
  });
});
