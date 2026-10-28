// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: SessionNotificationSettings } =
  await import('../sessions/SessionNotificationSettings.svelte');

describe('SessionNotificationSettings', () => {
  let component;

  function render(props = {}) {
    component = mount(SessionNotificationSettings, {
      target: document.body,
      props: { agentId: 'main', sessionId: 'ses_1', ...props },
    });
    flushSync();
  }

  const toggle = () => document.querySelector('button[aria-expanded]');
  const select = () =>
    document.querySelector(`button[aria-label="${t('notifications.label')}"]`);

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    rpcMock.mockReset();
  });

  afterEach(() => {
    if (component) unmount(component);
    component = null;
  });

  it('keeps the setting folded behind Advanced until it is opened', () => {
    render();
    expect(toggle().textContent).toContain(t('notifications.advanced'));
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(select()).toBeNull();
    expect(document.body.textContent).not.toContain(
      t('notifications.sessionHelp'),
    );

    toggle().click();
    flushSync();
    expect(toggle().getAttribute('aria-expanded')).toBe('true');
    expect(select()).not.toBeNull();
    expect(document.body.textContent).toContain(t('notifications.sessionHelp'));
  });

  it('shows a non-default policy on the folded row, and saves a change from the open fold', async () => {
    rpcMock.mockResolvedValue({ session: { notification_policy: 'muted' } });
    const onCommit = vi.fn();
    render({ onCommit });
    expect(toggle().textContent).not.toContain(t('notifications.all'));

    toggle().click();
    flushSync();
    select().click();
    flushSync();
    [...document.querySelectorAll('[role="option"]')]
      .find((option) => option.textContent.trim() === t('notifications.muted'))
      .click();
    flushSync();
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledTimes(1));
    expect(rpcMock).toHaveBeenCalledWith('session.notification_policy', {
      agent_id: 'main',
      session_id: 'ses_1',
      policy: 'muted',
    });

    toggle().click();
    flushSync();
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(toggle().textContent).toContain(
      t('notifications.hint', { policy: t('notifications.muted') }),
    );
  });

  it('keeps the fold open while a failed save shows its error', async () => {
    rpcMock.mockRejectedValue(new Error('Save failed'));
    render();
    toggle().click();
    flushSync();
    select().click();
    flushSync();
    [...document.querySelectorAll('[role="option"]')]
      .find((option) => option.textContent.trim() === t('notifications.muted'))
      .click();
    flushSync();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('Save failed'),
    );

    toggle().click();
    flushSync();
    expect(toggle().getAttribute('aria-expanded')).toBe('true');
    expect(document.body.textContent).toContain('Save failed');
  });
});
