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

const { default: SessionAdvancedSettings } =
  await import('../sessions/SessionAdvancedSettings.svelte');

describe('SessionAdvancedSettings', () => {
  let component;

  function render(props = {}) {
    component = mount(SessionAdvancedSettings, {
      target: document.body,
      props: { agentId: 'main', sessionId: 'ses_1', ...props },
    });
    flushSync();
  }

  const toggle = () => document.querySelector('button[aria-expanded]');
  const select = () =>
    document.querySelector(`button[aria-label="${t('notifications.label')}"]`);
  const button = (text) =>
    [...document.querySelectorAll('button')].find(
      (node) => node.textContent.trim() === text,
    );
  const open = () => {
    toggle().click();
    flushSync();
  };

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
    expect(toggle().textContent).toContain(t('sessions.advanced'));
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(select()).toBeNull();
    expect(document.body.textContent).not.toContain(
      t('notifications.sessionHelp'),
    );
    expect(button(t('sessions.rename'))).toBeUndefined();
    expect(button(t('sessions.compactionPolicy'))).toBeUndefined();

    toggle().click();
    flushSync();
    expect(toggle().getAttribute('aria-expanded')).toBe('true');
    expect(select()).not.toBeNull();
    expect(document.body.textContent).toContain(t('notifications.sessionHelp'));
    expect(button(t('sessions.rename'))).toBeDefined();
    expect(button(t('sessions.compactionPolicy'))).toBeDefined();
  });

  it('renames the displayed Session from its automatic title and preserves an untouched title', async () => {
    const onChanged = vi.fn();
    rpcMock.mockResolvedValue({ ok: true });
    render({
      session: { id: 'ses_1', auto_title: 'Weekly sync' },
      onChanged,
    });
    open();
    const input = () => document.querySelector('input[type="text"]');
    button(t('sessions.rename')).click();
    flushSync();
    expect(input().value).toBe('Weekly sync');
    button(t('common.save')).click();
    flushSync();
    expect(input()).toBeNull();
    expect(rpcMock).not.toHaveBeenCalled();

    button(t('sessions.rename')).click();
    flushSync();
    input().value = 'Release planning';
    input().dispatchEvent(new Event('input', { bubbles: true }));
    input().dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
    );
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
    expect(rpcMock).toHaveBeenCalledWith('session.rename', {
      agent_id: 'main',
      session_id: 'ses_1',
      title: 'Release planning',
    });
    flushSync();
    expect(input()).toBeNull();
  });

  it.each([false, true])(
    'edits a Session Policy with an existing override: %s',
    async (overridden) => {
      const policy = {
        enabled: true,
        trigger: { type: 'input_tokens', tokens: 40000 },
        strategy: { type: 'continuation' },
      };
      const effective = { ...policy, enabled: false };
      const onChanged = vi.fn();
      const onCompactionPolicyChange = vi.fn();
      rpcMock.mockResolvedValue({
        override: overridden ? null : effective,
        effective,
      });
      render({
        agentId: 'worker@project',
        session: {
          id: 'ses_1',
          compaction_policy_effective: policy,
          compaction_policy_override: overridden ? policy : null,
        },
        onChanged,
        onCompactionPolicyChange,
      });
      open();
      button(t('sessions.compactionPolicy')).click();
      flushSync();
      const override = document.querySelector(
        `[role="switch"][aria-label="${t('sessions.compactionOverride')}"]`,
      );
      const enabled = () =>
        document.querySelector(
          `[role="switch"][aria-label="${t('compaction.enabled')}"]`,
        );
      expect(override.getAttribute('aria-checked')).toBe(String(overridden));
      expect(enabled().disabled).toBe(!overridden);
      override.click();
      flushSync();
      if (!overridden) enabled().click();
      flushSync();
      button(t('common.save')).click();
      await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
      expect(rpcMock).toHaveBeenCalledWith('session.set_compaction_policy', {
        agent_id: 'worker@project',
        session_id: 'ses_1',
        policy: overridden ? null : effective,
      });
      expect(onCompactionPolicyChange).toHaveBeenCalledWith(
        'worker@project',
        'ses_1',
        effective,
      );
      flushSync();
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    },
  );

  it('retains the rename draft after a failure and allows a retry', async () => {
    rpcMock
      .mockRejectedValueOnce(new Error('Rename failed'))
      .mockResolvedValue({ ok: true });
    const onChanged = vi.fn();
    render({ session: { id: 'ses_1', title: 'Before' }, onChanged });
    open();
    button(t('sessions.rename')).click();
    flushSync();
    const input = document.querySelector('input[type="text"]');
    input.value = 'After';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    button(t('common.save')).click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]')).not.toBeNull(),
    );
    expect(input.value).toBe('After');
    expect(onChanged).not.toHaveBeenCalled();
    button(t('common.save')).click();
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
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
