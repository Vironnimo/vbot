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
const PROFILES = [
  { name: 'full_access', builtin: true, restricted: false },
  { name: 'restricted', builtin: true, restricted: true },
  { name: 'Reviewer', builtin: false, restricted: true },
];

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
      session_read_folders: [{ computer_id: 'server', folder: '/library' }],
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
    const rule = t('permissions.session.readFolder', {
      folder: '/library',
      computer: 'server',
    });
    list()
      .querySelector(
        '[aria-label="' +
          t('permissions.session.grantRemoveRule', { rule }) +
          '"]',
      )
      .click();
    await settle();
    expect(removeGrantsMock).toHaveBeenLastCalledWith({
      agentId: 'coder',
      sessionId: 's1',
      readFolder: '/library',
      computerId: 'server',
    });
  });

  it('gives a Session a profile, asking before full access lifts inherited limits', async () => {
    const restricted = {
      restricted: true,
      layers: [OWN_LAYER, CRON_LAYER],
      stored_ceiling: true,
      profiles: PROFILES,
      agent_profile: 'Reviewer',
      session_profile: null,
      session_rules: null,
    };
    permissionsMock.mockResolvedValueOnce(restricted);
    permissionsMock.mockResolvedValueOnce({
      ...restricted,
      session_profile: 'restricted',
    });
    permissionsMock.mockResolvedValueOnce({
      restricted: false,
      layers: [],
      stored_ceiling: false,
      profiles: PROFILES,
      agent_profile: 'Reviewer',
      session_profile: 'full_access',
      session_rules: null,
    });
    setMock.mockResolvedValue({});
    await render({ sessionId: 's1' });

    const choice = (value) =>
      document.querySelector(`[data-access-choice="${value}"]`);
    const lockText = () =>
      document.querySelector('[data-session-access-label]').textContent.trim();
    // The default names the profile the Agent uses, and so does the lock.
    expect(choice('agent').getAttribute('aria-checked')).toBe('true');
    expect(choice('agent').textContent).toContain(
      t('permissions.session.agentDefault', { profile: 'Reviewer' }),
    );
    expect(lockText()).toBe('Reviewer');

    choice('profile:restricted').click();
    await settle();
    expect(setMock).toHaveBeenLastCalledWith({
      agentId: 'coder',
      sessionId: 's1',
      profile: 'restricted',
    });
    expect(choice('profile:restricted').getAttribute('aria-checked')).toBe(
      'true',
    );
    expect(lockText()).toBe(t('permissions.profile.restricted'));

    // Full access also removes the stored inherited limits: it asks first.
    choice('profile:full_access').click();
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
      profile: 'full_access',
    });
    expect(
      document
        .querySelector('[data-session-permissions]')
        .getAttribute('data-session-permissions'),
    ).toBe('full');

    // A profile deleted since stays shown as chosen, with why the Session stopped.
    permissionsMock.mockResolvedValue({
      restricted: true,
      layers: [],
      stored_ceiling: false,
      profiles: PROFILES,
      agent_profile: 'Reviewer',
      session_profile: 'Gone',
      session_profile_error: 'Gone no longer exists.',
    });
    document
      .querySelector('[data-session-permissions]')
      .dispatchEvent(new Event('pointerenter'));
    await settle();
    expect(choice('missing').getAttribute('aria-checked')).toBe('true');
    expect(choice('missing').textContent).toContain(
      t('permissions.profile.missing', { name: 'Gone' }),
    );
    expect(
      document.querySelector('[data-session-permissions-card]').textContent,
    ).toContain('Gone no longer exists.');
  });

  it('shows a draft its default access, without access choices', async () => {
    permissionsMock.mockResolvedValue({
      restricted: false,
      layers: [],
      profiles: PROFILES,
      agent_profile: 'full_access',
    });
    await render({ workingProjectId: null });

    const lock = document.querySelector('[data-session-permissions]');
    expect(lock.getAttribute('data-session-permissions')).toBe('full');
    expect(lock.textContent.trim()).toBe(t('permissions.preset.full_access'));
    expect(document.querySelector('[data-access-choice]')).toBeNull();
    expect(
      document.querySelector('[data-session-draft-access]').textContent,
    ).toContain(
      t('permissions.session.agentDefault', {
        profile: t('permissions.preset.full_access'),
      }),
    );
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
      card
        .querySelector('[data-session-permissions-details] .info-hint')
        .getAttribute('data-help-text'),
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

    const lock = document.querySelector('[data-session-permissions]');
    expect(lock.getAttribute('aria-label')).toContain(
      t('permissions.session.badge'),
    );
    expect(lock.textContent.trim()).toBe(t('permissions.session.badge'));
    expect(clearButton()).toBeUndefined();
  });
});
