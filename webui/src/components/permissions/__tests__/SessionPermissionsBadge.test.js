// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const permissionsMock = vi.fn();
const clearMock = vi.fn();
vi.mock('$lib/api.js', () => ({
  getSessionPermissions: (...args) => permissionsMock(...args),
  clearSessionCeiling: (...args) => clearMock(...args),
}));

const { default: SessionPermissionsBadge } =
  await import('../SessionPermissionsBadge.svelte');

const OWN_LAYER = {
  source: 'agent',
  workdir: '/work',
  read: ['/work'],
  write: ['/work'],
  commands: null,
  web: null,
  deny: [],
};
const CRON_LAYER = { ...OWN_LAYER, source: 'cron', write: [] };

async function settle() {
  await vi.runAllTimersAsync();
  flushSync();
}

describe('SessionPermissionsBadge', () => {
  let component = null;

  async function render(props) {
    component = mount(SessionPermissionsBadge, {
      target: document.body,
      props: { agentAddress: 'coder', ...props },
    });
    flushSync();
    await settle();
  }

  const clearButton = () =>
    [...document.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === t('permissions.session.clear'),
    );

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
    init('en');
    permissionsMock.mockReset();
    clearMock.mockReset();
  });

  afterEach(async () => {
    if (component) await unmount(component);
    component = null;
    vi.useRealTimers();
    document.body.innerHTML = '';
  });

  it('removes the limits a Session inherited after a confirmation', async () => {
    permissionsMock.mockResolvedValueOnce({
      restricted: true,
      layers: [OWN_LAYER, CRON_LAYER],
      stored_ceiling: true,
    });
    permissionsMock.mockResolvedValueOnce({
      restricted: true,
      layers: [OWN_LAYER],
      stored_ceiling: false,
    });
    clearMock.mockResolvedValue({ cleared: true });
    await render({ sessionId: 's1' });

    const card = document.querySelector('[data-session-permissions-card]');
    expect(card.textContent).toContain(t('permissions.session.source.cron'));
    clearButton().click();
    flushSync();
    expect(clearMock).not.toHaveBeenCalled();
    const confirm = [...document.querySelectorAll('button')].find(
      (button) =>
        button.textContent.trim() === t('permissions.session.clearConfirm'),
    );
    confirm.click();
    await settle();

    expect(clearMock).toHaveBeenCalledWith({
      agentId: 'coder',
      sessionId: 's1',
    });
    expect(permissionsMock).toHaveBeenCalledTimes(2);
    expect(clearButton()).toBeUndefined();
    expect(document.querySelector('[data-session-permissions]')).not.toBeNull();
  });

  it('offers no removal for limits the Session does not store', async () => {
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [OWN_LAYER],
      stored_ceiling: false,
    });
    await render({ sessionId: 's1' });

    expect(document.querySelector('[data-session-permissions]')).not.toBeNull();
    expect(clearButton()).toBeUndefined();
  });
});
