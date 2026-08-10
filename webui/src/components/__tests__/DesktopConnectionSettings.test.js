// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';

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
    expect(buttonsByText('Connect')).toHaveLength(1);
    expect(buttonsByText('Remove')).toHaveLength(1);

    buttonsByText('Connect')[0].click();
    flushSync();
    await waitForText('Server unreachable — Try again.');
    expect(selectServer).toHaveBeenCalledWith('office.lan', 9000);

    buttonsByText('Remove')[0].click();
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
    await waitForText('No saved servers');

    setInput('desktop-settings-server-host', 'pi.lan');
    setInput('desktop-settings-server-port', '9000');
    setInput('desktop-settings-server-label', 'Pi');
    buttonsByText('Add server')[0].click();
    flushSync();

    await waitForCondition(() => addServer.mock.calls.length === 1);
    expect(addServer).toHaveBeenCalledWith('pi.lan', 9000, 'Pi');
    await waitForText('pi.lan:9000');
  });
});

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
