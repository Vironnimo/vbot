import { describe, expect, it } from 'vitest';

import {
  CHANNEL_DM_SCOPE_MAIN,
  CHANNEL_DM_SCOPE_PER_CONVERSATION,
  applyChannelList,
  buildCreatePayload,
  buildUpdatePayload,
  createChannelSettingsState,
} from '../channelSettings.js';

describe('channelSettings helpers', () => {
  it('creates the default state shape', () => {
    expect(createChannelSettingsState()).toEqual({
      channels: [],
      loading: false,
      error: null,
      selectedChannelId: null,
    });
  });

  it('normalizes channel list entries and keeps selected channel when present', () => {
    const state = {
      ...createChannelSettingsState(),
      loading: true,
      error: 'failed',
      selectedChannelId: 'tg-assistant-b',
    };

    const next = applyChannelList(state, [
      {
        id: 'tg-assistant-b',
        platform: 'telegram',
        agent_id: 'assistant-b',
        dm_scope: 'main',
        allowed_chat_ids: ['12345', -100],
        token_env_var: 'TELEGRAM_BOT_TOKEN_B',
        enabled: false,
        running: 'true',
      },
      {
        id: 'tg-assistant-a',
        platform: 'telegram',
        agent_id: 'assistant-a',
        token_env_var: 'TELEGRAM_BOT_TOKEN_A',
        allowed_chat_ids: [777],
      },
    ]);

    expect(next.loading).toBe(false);
    expect(next.error).toBeNull();
    expect(next.selectedChannelId).toBe('tg-assistant-b');
    expect(next.channels.map((channel) => channel.id)).toEqual([
      'tg-assistant-a',
      'tg-assistant-b',
    ]);
    expect(next.channels[1]).toMatchObject({
      dm_scope: 'main',
      allowed_chat_ids: [12345, -100],
      enabled: false,
      running: true,
    });
  });

  it('builds create payloads with defaults, parsed chat ids, and boolean coercion', () => {
    expect(
      buildCreatePayload({
        id: 'tg-assistant',
        platform: 'telegram',
        agent_id: 'assistant',
        token_env_var: 'TELEGRAM_BOT_TOKEN_TG_ASSISTANT',
      }),
    ).toEqual({
      id: 'tg-assistant',
      platform: 'telegram',
      agent_id: 'assistant',
      dm_scope: CHANNEL_DM_SCOPE_PER_CONVERSATION,
      allowed_chat_ids: [],
      token_env_var: 'TELEGRAM_BOT_TOKEN_TG_ASSISTANT',
      enabled: true,
    });

    expect(
      buildCreatePayload({
        id: 'tg-assistant',
        platform: 'telegram',
        agent_id: 'assistant',
        dm_scope: CHANNEL_DM_SCOPE_MAIN,
        allowed_chat_ids: '12345, -100\n12345',
        token_env_var: 'TELEGRAM_BOT_TOKEN_TG_ASSISTANT',
        enabled: 'false',
      }),
    ).toEqual({
      id: 'tg-assistant',
      platform: 'telegram',
      agent_id: 'assistant',
      dm_scope: CHANNEL_DM_SCOPE_MAIN,
      allowed_chat_ids: [12345, -100],
      token_env_var: 'TELEGRAM_BOT_TOKEN_TG_ASSISTANT',
      enabled: false,
    });
  });

  it('validates create payload input fields', () => {
    expect(() =>
      buildCreatePayload({
        id: 'tg-assistant',
        platform: 'discord',
        agent_id: 'assistant',
        token_env_var: 'TOKEN',
      }),
    ).toThrow(/platform must be one of/u);

    expect(() =>
      buildCreatePayload({
        id: 'tg-assistant',
        platform: 'telegram',
        agent_id: 'assistant',
        token_env_var: 'TOKEN',
        allowed_chat_ids: 'abc',
      }),
    ).toThrow(/allowed_chat_ids/u);
  });

  it('builds update payloads from partial form data', () => {
    expect(
      buildUpdatePayload({
        id: 'tg-assistant',
        enabled: true,
      }),
    ).toEqual({
      id: 'tg-assistant',
      enabled: true,
    });

    expect(
      buildUpdatePayload({
        id: 'tg-assistant',
        allowed_chat_ids: '1, -2',
      }),
    ).toEqual({
      id: 'tg-assistant',
      allowed_chat_ids: [1, -2],
    });
  });

  it('validates update payload inputs', () => {
    expect(() =>
      buildUpdatePayload({
        id: 'tg-assistant',
      }),
    ).toThrow(/At least one channel field is required/u);

    expect(() =>
      buildUpdatePayload({
        id: 'tg-assistant',
        dm_scope: 'invalid',
      }),
    ).toThrow(/dm_scope must be one of/u);
  });
});
