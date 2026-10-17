// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: DesktopConnectionSettings } =
  await import('../settings/DesktopConnectionSettings.svelte');

describe('DesktopConnectionSettings', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    window.history.replaceState({}, '', '/?accessor=desktop');
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
    }
    delete window.pywebview;
    document.body.innerHTML = '';
  });

  it('marks the active server and manages inactive remembered servers', async () => {
    const removeServer = vi.fn().mockResolvedValue({ removed: true });
    const selectServer = vi.fn().mockResolvedValue({
      status: 'server_unreachable',
      error_title: 'Server unreachable',
      error_body: 'Try again.',
    });
    window.pywebview = {
      api: {
        listServers: vi.fn().mockResolvedValue([
          { host: 'home.lan', port: 8420, label: 'Home', active: true },
          { host: 'office.lan', port: 9000, active: false },
        ]),
        removeServer,
        selectServer,
      },
    };

    mountedComponent = mount(DesktopConnectionSettings, {
      target: document.body,
    });
    flushSync();
    await waitForText('office.lan:9000');

    expect(document.body.textContent).toContain('Home');
    expect(
      document.querySelector('.desktop-server-row .chip.success'),
    ).toBeTruthy();
    const connect = t('settings.desktop.connection.connect');
    const remove = t('common.remove');
    expect(buttonsByText(connect)).toHaveLength(1);
    expect(buttonsByText(remove)).toHaveLength(1);

    buttonsByText(connect)[0].click();
    flushSync();
    await waitForText('Server unreachable — Try again.');
    expect(selectServer).toHaveBeenCalledWith('office.lan', 9000);

    buttonsByText(remove)[0].click();
    flushSync();
    await waitForCondition(() => removeServer.mock.calls.length === 1);
    expect(removeServer).toHaveBeenCalledWith('office.lan', 9000);
  });

  it('adds a validated server without changing the active connection', async () => {
    const listServers = vi
      .fn()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        { host: 'pi.lan', port: 9000, label: 'Pi', active: false },
      ]);
    const addServer = vi.fn().mockResolvedValue({
      host: 'pi.lan',
      port: 9000,
      label: 'Pi',
    });
    window.pywebview = {
      api: {
        listServers,
        addServer,
      },
    };

    mountedComponent = mount(DesktopConnectionSettings, {
      target: document.body,
    });
    flushSync();
    await waitForText(t('settings.desktop.connection.emptyTitle'));

    // The add form stays closed until "Add server" opens it.
    expect(document.querySelector('form')).toBeNull();
    buttonsByText(t('settings.desktop.connection.addAction'))[0].click();
    await waitForCondition(
      () => document.activeElement?.id === 'desktop-settings-server-host',
    );

    setInput('desktop-settings-server-host', 'pi.lan');
    setInput('desktop-settings-server-port', '9000');
    setInput('desktop-settings-server-label', 'Pi');
    document.querySelector('form button[type="submit"]').click();
    flushSync();

    await waitForCondition(() => addServer.mock.calls.length === 1);
    expect(addServer).toHaveBeenCalledWith('pi.lan', 9000, 'Pi');
    await waitForText('pi.lan:9000');
    await waitForCondition(() => document.querySelector('form') === null);
  });

  it('shows https servers by their address and saves one with its own port', async () => {
    const cloud = {
      host: 'https://vbot.example.com',
      port: 443,
      scheme: 'https',
    };
    // The same host over plain HTTP on port 443 is another server.
    const plain = { host: 'vbot.example.com', port: 443, scheme: 'http' };
    const added = {
      host: 'https://office.example.com:8443',
      port: 8443,
      scheme: 'https',
      label: 'Office',
    };
    const listServers = vi
      .fn()
      .mockResolvedValueOnce([{ ...cloud, active: true }, plain])
      .mockResolvedValueOnce([{ ...cloud, active: true }, plain, added]);
    const addServer = vi.fn().mockResolvedValue(added);
    window.pywebview = { api: { listServers, addServer } };

    mountedComponent = mount(DesktopConnectionSettings, {
      target: document.body,
    });
    flushSync();
    await waitForText('vbot.example.com:443');

    const addresses = () =>
      [...document.querySelectorAll('.desktop-server-row__address')].map(
        (node) => node.textContent.trim(),
      );
    expect(addresses()).toEqual([
      'https://vbot.example.com',
      'vbot.example.com:443',
    ]);

    buttonsByText(t('settings.desktop.connection.addAction'))[0].click();
    await waitForCondition(
      () => document.activeElement?.id === 'desktop-settings-server-host',
    );
    setInput('desktop-settings-server-host', 'https://office.example.com:8443');
    setInput('desktop-settings-server-label', 'Office');
    flushSync();
    expect(
      document.getElementById('desktop-settings-server-port').disabled,
    ).toBe(true);
    document.querySelector('form button[type="submit"]').click();
    flushSync();

    await waitForCondition(() => addServer.mock.calls.length === 1);
    expect(addServer).toHaveBeenCalledWith(
      'https://office.example.com:8443',
      null,
      'Office',
    );
    await waitForText('https://office.example.com:8443');
  });

  it('changes nothing and reports nothing when the user declines in the Desktop', async () => {
    const declined = () => Promise.reject(new Error('declined'));
    const selectServer = vi.fn(declined);
    const removeServer = vi.fn(declined);
    const addServer = vi.fn(declined);
    window.pywebview = {
      api: {
        listServers: vi.fn().mockResolvedValue([
          { host: 'home.lan', port: 8420, active: true },
          { host: 'office.lan', port: 9000, active: false },
        ]),
        selectServer,
        removeServer,
        addServer,
      },
    };

    mountedComponent = mount(DesktopConnectionSettings, {
      target: document.body,
    });
    flushSync();
    await waitForText('office.lan:9000');

    buttonsByText(t('settings.desktop.connection.connect'))[0].click();
    flushSync();
    await waitForCondition(() => selectServer.mock.calls.length === 1);
    await settle();
    buttonsByText(t('common.remove'))[0].click();
    flushSync();
    await waitForCondition(() => removeServer.mock.calls.length === 1);
    await settle();
    buttonsByText(t('settings.desktop.connection.addAction'))[0].click();
    await waitForCondition(
      () => document.activeElement?.id === 'desktop-settings-server-host',
    );
    setInput('desktop-settings-server-host', 'pi.lan');
    document.querySelector('form button[type="submit"]').click();
    flushSync();
    await waitForCondition(() => addServer.mock.calls.length === 1);
    await settle();

    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(document.body.textContent).toContain('office.lan:9000');
    // The add form stays open with what the user typed.
    expect(document.getElementById('desktop-settings-server-host').value).toBe(
      'pi.lan',
    );
  });
});

// Let a rejected bridge call reach its handler and the view update.
async function settle() {
  await new Promise((resolve) => setTimeout(resolve, 0));
  flushSync();
}

function setInput(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function buttonsByText(text) {
  return [...document.querySelectorAll('button')].filter(
    (button) => button.textContent.trim() === text,
  );
}

async function waitForText(text) {
  await waitForCondition(() => document.body.textContent.includes(text));
}

async function waitForCondition(predicate) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (predicate()) {
      return;
    }
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    flushSync();
  }
  throw new Error('Condition not met');
}
