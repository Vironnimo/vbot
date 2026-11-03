// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../../lib/i18n.js';
import { reactiveProps } from '../../__tests__/reactiveProps.support.svelte.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const permissionsMock = vi.fn();
const clearMock = vi.fn();
const setMock = vi.fn();
const removeGrantsMock = vi.fn();
vi.mock('$lib/api.js', () => ({
  removeSessionGrants: (...args) => removeGrantsMock(...args),
  getSessionPermissions: (...args) => permissionsMock(...args),
  clearSessionCeiling: (...args) => clearMock(...args),
  setSessionPermissions: (...args) => setMock(...args),
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
    setMock.mockReset();
    removeGrantsMock.mockReset();
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

  it('lists the rules allowed while approving, each removable', async () => {
    const grants = ['Shell(git push *)', 'Edit(/notes)'];
    permissionsMock.mockResolvedValueOnce({
      restricted: true,
      layers: [OWN_LAYER],
      session_grants: grants,
    });
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [OWN_LAYER],
      session_grants: ['Edit(/notes)'],
    });
    removeGrantsMock.mockResolvedValue({ session_grants: ['Edit(/notes)'] });
    await render({ sessionId: 's1' });

    const list = () => document.querySelector('[data-session-grants]');
    expect(list().textContent).toContain('Shell(git push *)');
    list()
      .querySelector(
        `[aria-label="${t('permissions.session.grantRemoveRule', { rule: grants[0] })}"]`,
      )
      .click();
    await settle();

    expect(removeGrantsMock).toHaveBeenCalledWith({
      agentId: 'coder',
      sessionId: 's1',
      rules: ['Shell(git push *)'],
    });
    expect(list().textContent).not.toContain('Shell(git push *)');
    expect(list().textContent).toContain('Edit(/notes)');
  });

  it('gives a Session its own access, asking before full access lifts inherited limits', async () => {
    const restricted = {
      restricted: true,
      layers: [OWN_LAYER, CRON_LAYER],
      stored_ceiling: true,
      session_rules: null,
      session_preset: null,
    };
    permissionsMock.mockResolvedValueOnce(restricted);
    permissionsMock.mockResolvedValueOnce({
      ...restricted,
      session_rules: { read: 'work_area', write: 'work_area' },
      session_preset: 'work_dir',
    });
    permissionsMock.mockResolvedValue({
      restricted: false,
      layers: [],
      stored_ceiling: false,
      session_rules: { read: 'anywhere' },
      session_preset: 'full_access',
    });
    setMock.mockResolvedValue({});
    await render({ sessionId: 's1' });

    const choice = (value) =>
      document.querySelector(`[data-access-choice="${value}"]`);
    expect(choice('agent').getAttribute('aria-checked')).toBe('true');

    choice('work_dir').click();
    await settle();
    expect(setMock).toHaveBeenLastCalledWith({
      agentId: 'coder',
      sessionId: 's1',
      preset: 'work_dir',
    });
    expect(choice('work_dir').getAttribute('aria-checked')).toBe('true');

    // Full access also removes the stored inherited limits: it asks first.
    choice('full_access').click();
    flushSync();
    expect(setMock).toHaveBeenCalledTimes(1);
    [...document.querySelectorAll('button')]
      .find(
        (button) =>
          button.textContent.trim() === t('permissions.session.fullConfirm'),
      )
      .click();
    await settle();
    expect(setMock).toHaveBeenLastCalledWith({
      agentId: 'coder',
      sessionId: 's1',
      preset: 'full_access',
    });
    expect(
      document
        .querySelector('[data-session-permissions]')
        .getAttribute('data-session-permissions'),
    ).toBe('full');
  });

  it('shows a draft with full access nothing', async () => {
    permissionsMock.mockResolvedValue({ restricted: false, layers: [] });
    await render({ workingProjectId: null });

    expect(document.querySelector('[data-session-permissions]')).toBeNull();
  });

  it('names the scratch area and states the limits of the checks', async () => {
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [
        {
          ...OWN_LAYER,
          read: ['/work', '{scratch}'],
          write: ['/work', '{scratch}'],
          web: ['github.com'],
        },
      ],
      stored_ceiling: false,
    });
    await render({ sessionId: 's1' });

    const card = document.querySelector('[data-session-permissions-card]');
    const values = [...card.querySelectorAll('dd')].map((dd) =>
      dd.textContent.replace(/\s+/g, ' ').trim(),
    );
    expect(values).toContain(`/work, ${t('permissions.session.scratch')}`);
    expect(card.textContent).not.toContain('{scratch}');
    const labels = [...card.querySelectorAll('dt')].map((dt) =>
      dt.textContent.trim(),
    );
    expect(labels).toContain(t('permissions.preview.web'));
    expect(card.textContent).toContain(t('permissions.limits'));
    expect(
      card.querySelector('.info-hint').getAttribute('data-help-text'),
    ).toBe(t('permissions.limitsHelp'));

    // A rule below the scratch area keeps the placeholder, explained once.
    await unmount(component);
    document.body.innerHTML = '';
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [{ ...OWN_LAYER, deny: ['Read({scratch}/keys)'] }],
      stored_ceiling: false,
    });
    await render({ sessionId: 's1' });
    expect(
      document.querySelector('[data-session-permissions-card]').textContent,
    ).toContain(t('permissions.preview.scratch', { scratch: '{scratch}' }));
  });

  it('reads the limits again after they may have changed elsewhere', async () => {
    permissionsMock.mockResolvedValue({ restricted: false, layers: [] });
    const props = reactiveProps({
      agentAddress: 'coder',
      sessionId: 's1',
      revision: '0',
    });
    component = mount(SessionPermissionsBadge, {
      target: document.body,
      props,
    });
    await settle();
    expect(
      document
        .querySelector('[data-session-permissions]')
        .getAttribute('data-session-permissions'),
    ).toBe('full');

    // The Agent or the Session changed on the server.
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [OWN_LAYER],
      stored_ceiling: false,
    });
    props.revision = '1';
    await settle();
    expect(
      document
        .querySelector('[data-session-permissions]')
        .getAttribute('data-session-permissions'),
    ).toBe('restricted');

    // A change no event reports (the shared default) shows once the card
    // is about to open.
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [OWN_LAYER, CRON_LAYER],
      stored_ceiling: false,
    });
    document
      .querySelector('[data-session-permissions]')
      .dispatchEvent(new Event('pointerenter'));
    await settle();
    expect(
      document.querySelector('[data-session-permissions-card]').textContent,
    ).toContain(t('permissions.session.source.cron'));
    expect(permissionsMock).toHaveBeenCalledTimes(3);
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
