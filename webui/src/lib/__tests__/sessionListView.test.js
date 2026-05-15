import { describe, expect, it } from 'vitest';

import {
  applySessionList,
  createSessionListState,
  selectSession,
  sessionDisplayName,
} from '../sessionListView.js';

describe('sessionListView helpers', () => {
  it('creates the default state shape', () => {
    expect(createSessionListState()).toEqual({
      sessions: [],
      loading: false,
      error: null,
      selectedSessionId: null,
    });
  });

  it('normalizes session lists, sorts by last activity, and preserves selected session', () => {
    const state = {
      ...createSessionListState(),
      loading: true,
      error: 'failed',
      selectedSessionId: 'channel-session',
    };

    const next = applySessionList(state, [
      {
        id: 'channel-session',
        platform: 'telegram',
        platform_conv_id: '12345',
        source_channel_id: 'tg-assistant',
        last_active_at: '2026-05-15T10:00:00+00:00',
      },
      {
        id: 'plain-session',
        last_active_at: '2026-05-15T11:00:00+00:00',
      },
    ]);

    expect(next.loading).toBe(false);
    expect(next.error).toBeNull();
    expect(next.selectedSessionId).toBe('channel-session');
    expect(next.sessions.map((session) => session.id)).toEqual([
      'plain-session',
      'channel-session',
    ]);
    expect(next.sessions[0]).toMatchObject({
      display_name: 'plain-session',
      is_channel_session: false,
    });
    expect(next.sessions[1]).toMatchObject({
      display_name: 'telegram/12345',
      source_channel_id: 'tg-assistant',
      is_channel_session: true,
    });
  });

  it('clears selected session when the session list no longer contains it', () => {
    const state = {
      ...createSessionListState(),
      selectedSessionId: 'missing-session',
    };

    const next = applySessionList(state, [{ id: 'known-session' }]);

    expect(next.selectedSessionId).toBeNull();
  });

  it('selects only existing sessions and clears unknown selections', () => {
    const state = {
      ...createSessionListState(),
      sessions: [{ id: 'first' }, { id: 'second' }],
    };

    expect(selectSession(state, 'second').selectedSessionId).toBe('second');
    expect(selectSession(state, 'unknown').selectedSessionId).toBeNull();
    expect(selectSession(state, '').selectedSessionId).toBeNull();
  });

  it('derives stable display names for channel and generic sessions', () => {
    expect(
      sessionDisplayName({
        platform: 'telegram',
        platform_conv_id: '-100123',
      }),
    ).toBe('telegram/-100123');
    expect(sessionDisplayName({ id: 'session-001' })).toBe('session-001');
    expect(sessionDisplayName({})).toBe('Session');
  });
});
