// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount } from 'svelte';
import { reactiveProps } from './_reactiveProps.svelte.js';

import {
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
  SettingsView,
  submitChannelForm,
  waitForCondition,
} from './SettingsView.support.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

describe('SettingsView', () => {
  let mountedComponent;

  beforeEach(() => {
    resetSettingsViewHarness();
    mountedComponent = null;
  });

  afterEach(async () => {
    mountedComponent = await cleanupSettingsViewHarness(mountedComponent);
  });

  it('loads channels panel and resolves running status for each channel', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [
          channelConfig('tg-assistant', {
            agent_id: 'assistant',
            enabled: true,
            dm_scope: 'per_conversation',
          }),
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
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    await waitForCondition(() => document.body.textContent.includes('tg-work'));

    expect(document.body.textContent).toContain('tg-assistant');
    expect(document.body.textContent).toContain('tg-work');
    expect(rpcMock).toHaveBeenCalledWith('channel.list');
    const channelsSection = document.body.querySelector(
      '[data-settings-section="channels"]',
    );
    expect(
      [...channelsSection.querySelectorAll('button')].some(
        (button) => button.textContent.trim() === 'Refresh',
      ),
    ).toBe(false);
    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.status' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);
    expect(
      rpcMock.mock.calls.some(
        (call) => call[0] === 'channel.status' && call[1]?.id === 'tg-work',
      ),
    ).toBe(true);
  });

  it('reloads an external channel change after an open form closes', async () => {
    const props = reactiveProps({ channelsRefreshToken: 0 });
    rpcMock.mockImplementation(
      createSettingsRpcMock({ channels: [channelConfig('tg-assistant')] }),
    );

    mountedComponent = mount(SettingsView, {
      target: document.body,
      props,
    });
    flushSync();
    await openChannelsPanel();
    const initialListCalls = rpcMock.mock.calls.filter(
      (call) => call[0] === 'channel.list',
    ).length;

    buttonByText('Add channel').click();
    flushSync();
    props.channelsRefreshToken += 1;
    flushSync();
    await Promise.resolve();

    expect(
      rpcMock.mock.calls.filter((call) => call[0] === 'channel.list'),
    ).toHaveLength(initialListCalls);

    buttonByText('Cancel').click();
    flushSync();
    await waitForCondition(
      () =>
        rpcMock.mock.calls.filter((call) => call[0] === 'channel.list')
          .length ===
        initialListCalls + 1,
    );
  });

  it('lists denied chats and allows one from the channel card', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [
          channelConfig('tg-assistant', {
            agent_id: 'assistant',
            enabled: true,
            allowed_chat_ids: [12345],
          }),
        ],
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
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    await waitForCondition(() =>
      document.body.textContent.includes('Julian B.'),
    );
    expect(document.body.textContent).toContain(
      'Recent requests from chats not on the allowlist',
    );
    expect(document.body.textContent).toContain('ID 99999');

    buttonByAriaLabel('Allow chat 99999').click();

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.update'),
    );

    const updateCall = rpcMock.mock.calls.find(
      (call) => call[0] === 'channel.update',
    );
    expect(updateCall[1]).toEqual({
      id: 'tg-assistant',
      allowed_chat_ids: ['12345', '99999'],
    });
  });

  it('creates a channel from the inline form', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [],
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

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

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.create' &&
          call[1]?.id === 'tg-new' &&
          call[1]?.platform === 'telegram' &&
          call[1]?.agent_id === 'assistant' &&
          call[1]?.dm_scope === 'main' &&
          call[1]?.token_env_var === 'TELEGRAM_BOT_TOKEN_TG_NEW' &&
          JSON.stringify(call[1]?.allowed_chat_ids) ===
            JSON.stringify([12345, -100123]),
      ),
    ).toBe(true);
  });

  it('updates, toggles, and deletes channels from row actions', async () => {
    rpcMock.mockImplementation(
      createSettingsRpcMock({
        channels: [
          channelConfig('tg-assistant', {
            agent_id: 'assistant',
            enabled: true,
          }),
        ],
      }),
    );

    mountedComponent = mount(SettingsView, { target: document.body });
    flushSync();
    await openChannelsPanel();

    await waitForCondition(() =>
      document.body.textContent.includes('tg-assistant'),
    );

    buttonByAriaLabel('Edit channel tg-assistant').click();
    flushSync();

    setInputValue('#channel-token-env-input', 'TELEGRAM_BOT_TOKEN_UPDATED');
    submitChannelForm();

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.update'),
    );

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.update' &&
          call[1]?.id === 'tg-assistant' &&
          call[1]?.token_env_var === 'TELEGRAM_BOT_TOKEN_UPDATED',
      ),
    ).toBe(true);

    buttonByAriaLabel('Disable channel tg-assistant').click();
    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.disable'),
    );

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.disable' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);

    buttonByAriaLabel('Delete channel tg-assistant').click();
    flushSync();

    // The row action opens the shared ConfirmDialog; the delete RPC only fires
    // once it is confirmed.
    expect(
      rpcMock.mock.calls.some((call) => call[0] === 'channel.delete'),
    ).toBe(false);
    confirmChannelDialog('Delete');

    await waitForCondition(() =>
      rpcMock.mock.calls.some((call) => call[0] === 'channel.delete'),
    );

    expect(
      rpcMock.mock.calls.some(
        (call) =>
          call[0] === 'channel.delete' && call[1]?.id === 'tg-assistant',
      ),
    ).toBe(true);
  });
});
