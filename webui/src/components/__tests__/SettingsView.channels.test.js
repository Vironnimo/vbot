// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';

import { t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import {
  agentsPayload,
  buttonByAriaLabel,
  buttonByText,
  channelConfig,
  cleanupSettingsViewHarness,
  confirmChannelDialog,
  createSettingsRpcMock,
  openChannelsPanel,
  openSimpleDropdown,
  resetSettingsViewHarness,
  rpcMock,
  selectSimpleOption,
  setInputValue,
  submitChannelForm,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

const { default: SettingsView } = await import('../SettingsView.svelte');

function callsTo(method) {
  return rpcMock.mock.calls.filter((call) => call[0] === method);
}

function waitForCall(method, params) {
  return waitForCondition(() =>
    callsTo(method).some(([, actual]) =>
      Object.entries(params).every(([key, value]) => actual?.[key] === value),
    ),
  );
}

describe('SettingsView Channels', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  async function openChannels(options, props = {}) {
    rpcMock.mockImplementation(createSettingsRpcMock(options));
    mountedComponent = mount(SettingsView, { target: document.body, props });
    flushSync();
    await openChannelsPanel();
  }

  it('retries a failed channel list, then lists channels and resolves the running status of each, without a manual refresh', async () => {
    let listFailure = new Error('channels offline');
    const backend = createSettingsRpcMock({
      channels: [
        channelConfig('tg-assistant', { agent_id: 'assistant' }),
        channelConfig('tg-work', {
          agent_id: 'assistant-work',
          enabled: false,
          dm_scope: 'main',
        }),
      ],
      channelStatuses: {
        'tg-assistant': { running: true, enabled: true },
        'tg-work': { running: false, enabled: false },
      },
    });
    rpcMock.mockImplementation((method, params) => {
      if (method === 'channel.list' && listFailure) {
        const error = listFailure;
        listFailure = null;
        return Promise.reject(error);
      }
      return backend(method, params);
    });
    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();
    const section = document.querySelector(
      '[data-settings-section="channels"]',
    );
    await waitForCondition(() => section.querySelector('[role="alert"]'));
    const alert = section.querySelector('[role="alert"]');
    expect(alert.textContent).toContain('channels offline');

    Array.from(alert.querySelectorAll('button'))
      .find((button) => button.textContent.trim() === t('common.retry'))
      .click();
    await waitForCondition(() => document.body.textContent.includes('tg-work'));
    expect(section.querySelector('[role="alert"]')).toBeNull();
    expect(document.body.textContent).toContain('tg-assistant');
    expect(callsTo('channel.list')).toHaveLength(2);
    expect(callsTo('channel.status').map(([, params]) => params.id)).toEqual(
      expect.arrayContaining(['tg-assistant', 'tg-work']),
    );
    expect(
      Array.from(
        document.querySelectorAll('[data-settings-section="channels"] button'),
      ).some((button) => button.textContent.trim() === 'Refresh'),
    ).toBe(false);
  });

  it('shows durable group participants and updates identity and roles', async () => {
    await openChannels({
      channels: [channelConfig('tg-assistant')],
      channelAccess: {
        'tg-assistant': {
          channel_id: 'tg-assistant',
          self_user_id: null,
          groups: [
            {
              access_scope_id: '-100',
              admin_user_ids: [],
              participants: [
                {
                  user_id: '50',
                  display_name: 'Alice',
                  last_seen_at: '2026-07-30T10:00:00+00:00',
                  role: 'member',
                },
                {
                  user_id: '51',
                  display_name: 'Bob',
                  last_seen_at: '2026-07-30T10:01:00+00:00',
                  role: 'member',
                },
              ],
            },
          ],
        },
      },
    });

    await waitForCall('channel.access.get', { id: 'tg-assistant' });
    await waitForCondition(() =>
      document.querySelector('.s-channel-access-group-title'),
    );
    expect(
      document.querySelector('.s-channel-access-group-title').textContent,
    ).toContain('-100');
    const rows = Array.from(
      document.querySelectorAll('.s-channel-access-row'),
    ).map((row) => row.textContent);
    expect(rows).toEqual([
      expect.stringMatching(/Alice[\s\S]*50/u),
      expect.stringMatching(/Bob[\s\S]*51/u),
    ]);

    buttonByAriaLabel('Use Alice as own identity').click();
    await waitForCall('channel.identity.set', {
      id: 'tg-assistant',
      user_id: '50',
    });
    const aliceIdentity = () => buttonByAriaLabel('Use Alice as own identity');
    await waitForCondition(
      () =>
        aliceIdentity().textContent.trim() === t('settings.channels.access.me'),
    );
    expect(aliceIdentity().disabled).toBe(true);
    expect(buttonByAriaLabel('Make Alice a member').disabled).toBe(true);

    buttonByAriaLabel('Make Bob an admin').click();
    await waitForCall('channel.admin.grant', {
      access_scope_id: '-100',
      user_id: '51',
    });
    await waitForCondition(() => buttonByAriaLabel('Make Bob a member'));

    buttonByAriaLabel('Make Bob a member').click();
    await waitForCall('channel.admin.revoke', {
      access_scope_id: '-100',
      user_id: '51',
    });
  });

  it('defers an external channel reload while a new channel form is open, not for an open row', async () => {
    const props = reactiveProps({ channelsRefreshToken: 0 });
    await openChannels({ channels: [channelConfig('tg-assistant')] }, props);
    const initialListCalls = callsTo('channel.list').length;

    buttonByText('Add channel').click();
    flushSync();
    setInputValue('#channel-id-input', 'tg-new');
    // A stray click on a row keeps the new Channel form.
    document.querySelector('.s-channel-card .s-entity__head').click();
    flushSync();
    expect(document.querySelector('#channel-id-input').value).toBe('tg-new');
    props.channelsRefreshToken += 1;
    flushSync();
    await Promise.resolve();
    expect(callsTo('channel.list')).toHaveLength(initialListCalls);

    buttonByText('Cancel').click();
    flushSync();
    await waitForCondition(
      () => callsTo('channel.list').length === initialListCalls + 1,
    );

    const disclosure = buttonByAriaLabel('Edit channel tg-assistant');
    disclosure.click();
    flushSync();
    props.channelsRefreshToken += 1;
    flushSync();
    await waitForCondition(
      () => callsTo('channel.list').length === initialListCalls + 2,
    );
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
  });

  it('names the Agents anew after an Agent change and keeps a new channel form', async () => {
    const agents = agentsPayload();
    const props = reactiveProps({ agentsRefreshToken: 0 });
    await openChannels(
      { channels: [channelConfig('tg-assistant')], agents },
      props,
    );
    buttonByText('Add channel').click();
    flushSync();
    setInputValue('#channel-id-input', 'tg-new');
    openSimpleDropdown('channel-agent-select');
    selectSimpleOption('channel-agent-select', 'Assistant');

    agents[0] = { ...agents[0], name: 'Front Desk' };
    props.agentsRefreshToken += 1;
    flushSync();
    await waitForCondition(() =>
      document
        .querySelector('.s-channel-card .s-row-desc')
        .textContent.includes('Front Desk'),
    );

    expect(
      document
        .getElementById('channel-agent-select')
        .querySelector('.dropdown-primitive__trigger-label')
        .textContent.trim(),
    ).toBe('Front Desk');
    expect(document.querySelector('#channel-id-input').value).toBe('tg-new');
  });

  it('lists denied chats and allows one from the channel card', async () => {
    await openChannels({
      channels: [channelConfig('tg-assistant', { allowed_chat_ids: [12345] })],
      channelStatuses: {
        'tg-assistant': {
          running: true,
          enabled: true,
          denied_chats: [
            {
              chat_id: '99999',
              kind: 'direct',
              display_name: 'Julian B.',
              last_seen_at: '2026-07-05T12:00:00+00:00',
              count: 3,
            },
          ],
        },
      },
    });

    await waitForCondition(() => document.querySelector('.s-channel-denied'));
    const denied = document.querySelector('.s-channel-denied');
    expect(denied.textContent).toContain(t('settings.channels.denied.title'));
    const rows = denied.querySelectorAll('.s-channel-denied-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('Julian B.');
    expect(rows[0].textContent).toContain('99999');

    // Allowing while the row is open must survive the form's next save.
    buttonByAriaLabel('Edit channel tg-assistant').click();
    flushSync();
    buttonByAriaLabel('Allow chat 99999').click();
    await waitForCondition(() => callsTo('channel.update').length > 0);
    expect(callsTo('channel.update')[0][1]).toEqual({
      id: 'tg-assistant',
      allowed_chat_ids: ['12345', '99999'],
    });
    await waitForCondition(
      () =>
        document.querySelector('#channel-allowed-chat-ids-input')?.value ===
        '12345, 99999',
    );
    setInputValue('#channel-token-env-input', 'TELEGRAM_BOT_TOKEN_UPDATED');
    buttonByText('Save').click();
    await waitForCall('channel.update', {
      token_env_var: 'TELEGRAM_BOT_TOKEN_UPDATED',
    });
    expect(callsTo('channel.update').at(-1)[1].allowed_chat_ids).toEqual([
      '12345',
      '99999',
    ]);
  });

  it('creates a channel from the inline form', async () => {
    await openChannels({ channels: [] });

    buttonByText('Add channel').click();
    flushSync();
    setInputValue('#channel-id-input', 'tg-new');
    openSimpleDropdown('channel-agent-select');
    selectSimpleOption('channel-agent-select', 'Assistant');
    openSimpleDropdown('channel-dm-scope-select');
    selectSimpleOption('channel-dm-scope-select', 'Main');
    setInputValue('#channel-token-env-input', 'TELEGRAM_BOT_TOKEN_TG_NEW');
    setInputValue('#channel-allowed-chat-ids-input', '12345, -100123');
    submitChannelForm();

    await waitForCondition(() => document.body.textContent.includes('tg-new'));
    expect(callsTo('channel.create')[0][1]).toMatchObject({
      id: 'tg-new',
      platform: 'telegram',
      agent_id: 'assistant',
      dm_scope: 'main',
      token_env_var: 'TELEGRAM_BOT_TOKEN_TG_NEW',
      allowed_chat_ids: ['12345', '-100123'],
    });
  });

  it('autosaves existing channel edits without closing the form, then disables and deletes after confirmation', async () => {
    await openChannels({
      channels: [channelConfig('tg-assistant', { agent_id: 'assistant' })],
    });
    await waitForCondition(() =>
      document.body.textContent.includes('tg-assistant'),
    );

    const disclosure = buttonByAriaLabel('Edit channel tg-assistant');
    disclosure.click();
    flushSync();
    expect(disclosure.getAttribute('aria-expanded')).toBe('true');
    setInputValue('#channel-token-env-input', 'TELEGRAM_BOT_TOKEN_UPDATED');
    // Real debounce: the channel form autosaves after its idle interval.
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(document.querySelector('#channel-token-env-input').value).toBe(
      'TELEGRAM_BOT_TOKEN_UPDATED',
    );
    await waitForCall('channel.update', {
      id: 'tg-assistant',
      token_env_var: 'TELEGRAM_BOT_TOKEN_UPDATED',
    });

    const enabled = buttonByAriaLabel('Enable channel tg-assistant');
    expect(enabled.getAttribute('aria-checked')).toBe('true');
    enabled.click();
    await waitForCall('channel.disable', { id: 'tg-assistant' });

    buttonByAriaLabel('Delete channel tg-assistant').click();
    flushSync();
    // The row action opens the shared ConfirmDialog; the delete RPC only fires
    // once it is confirmed.
    expect(callsTo('channel.delete')).toHaveLength(0);
    confirmChannelDialog('Delete');
    await waitForCall('channel.delete', { id: 'tg-assistant' });
  });
});
