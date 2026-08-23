import { describe, expect, it } from 'vitest';

import {
  applySessionList,
  createSessionListFilters,
  createSessionListState,
  isSessionHiddenByDefault,
  selectSession,
  sessionDisplayName,
  visibleSessionsForSelection,
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
        latest_completion_run_id: 'run-one',
        has_unread_completion: true,
        unread_run_id: 'run-one',
        unread_run_status: 'completed',
        unread_run_at: '2026-05-15T11:00:00+00:00',
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
      latest_completion_run_id: 'run-one',
      has_unread_completion: true,
      has_active_run: false,
      unread_run_id: 'run-one',
      unread_run_status: 'completed',
    });
    expect(next.sessions[1]).toMatchObject({
      display_name: 'telegram/12345',
      source_channel_id: 'tg-assistant',
      is_channel_session: true,
    });
  });

  it('normalizes sub-agent session metadata', () => {
    const next = applySessionList(createSessionListState(), [
      {
        id: 'child-session',
        is_subagent_session: true,
        subagent_parent: {
          agent_id: 'orchestrator',
          session_id: 'parent-session',
          run_id: 'parent-run',
          tool_call_id: 'tool-call-one',
          tool_call_index: 2,
        },
      },
    ]);

    expect(next.sessions[0]).toMatchObject({
      id: 'child-session',
      is_subagent_session: true,
      subagent_parent: {
        agent_id: 'orchestrator',
        session_id: 'parent-session',
        run_id: 'parent-run',
        tool_call_id: 'tool-call-one',
        tool_call_index: 2,
      },
    });
  });

  it('maps a fork_source object to is_fork and preserves it', () => {
    const forkSource = {
      agent_id: 'coder',
      session_id: 'source-session',
      project_id: null,
      forked_at: '2026-07-04T00:00:00+00:00',
      message_count: 12,
    };
    const next = applySessionList(createSessionListState(), [
      { id: 'fork-session', fork_source: forkSource },
    ]);

    expect(next.sessions[0]).toMatchObject({
      id: 'fork-session',
      is_fork: true,
      fork_source: forkSource,
    });
  });

  it('treats absent or non-object fork_source as not a fork', () => {
    const next = applySessionList(createSessionListState(), [
      { id: 'plain-session' },
      { id: 'bad-session', fork_source: 'nope' },
    ]);

    for (const session of next.sessions) {
      expect(session.is_fork).toBe(false);
      expect(session.fork_source).toBeNull();
    }
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

  it('prefers a user title over channel and id labels', () => {
    expect(
      sessionDisplayName({
        title: 'Release planning',
        platform: 'telegram',
        platform_conv_id: '-100123',
        id: 'session-001',
      }),
    ).toBe('Release planning');
    // A blank title falls back to the channel-derived label.
    expect(
      sessionDisplayName({
        title: '   ',
        platform: 'telegram',
        platform_conv_id: '-100123',
      }),
    ).toBe('telegram/-100123');
  });

  it('uses the automatic title beneath the manual override', () => {
    expect(
      sessionDisplayName({
        auto_title: 'Generated title',
        id: 'session-001',
      }),
    ).toBe('Generated title');
    expect(
      sessionDisplayName({
        title: 'Manual title',
        auto_title: 'Generated title',
        id: 'session-001',
      }),
    ).toBe('Manual title');
  });

  it('carries the title through normalization and into the display name', () => {
    const next = applySessionList(createSessionListState(), [
      {
        id: 'session-1',
        title: 'Release planning',
        platform: 'telegram',
        platform_conv_id: '999',
      },
    ]);

    expect(next.sessions[0]).toMatchObject({
      title: 'Release planning',
      display_name: 'Release planning',
    });
  });

  it('hides background-only and sub-agent sessions until their filter is enabled', () => {
    const next = applySessionList(createSessionListState(), [
      { id: 'user-session', run_kinds: ['user'] },
      { id: 'cron-session', run_kinds: ['cron'] },
      { id: 'reflection-session', run_kinds: ['reflection'] },
      { id: 'memory-reflection-session', run_kinds: ['memory_reflection'] },
      { id: 'skill-reflection-session', run_kinds: ['skill_reflection'] },
      { id: 'subagent-session', is_subagent_session: true },
      {
        id: 'linked-subagent-session',
        subagent_parent: {
          agent_id: 'parent-agent',
          session_id: 'parent-session',
        },
      },
      { id: 'mixed-session', run_kinds: ['cron', 'user'] },
      {
        id: 'channel-session',
        run_kinds: ['cron'],
        platform: 'telegram',
        platform_conv_id: '12345',
      },
    ]);

    expect(
      visibleSessionsForSelection(next.sessions).map((session) => session.id),
    ).toEqual(['channel-session', 'mixed-session', 'user-session']);

    expect(
      visibleSessionsForSelection(next.sessions, {
        filters: createSessionListFilters(),
      }).map((session) => session.id),
    ).toEqual(['channel-session', 'mixed-session', 'user-session']);

    expect(
      isSessionHiddenByDefault(
        next.sessions.find((session) => session.id === 'subagent-session'),
      ),
    ).toBe(true);
  });

  it('reveals each hidden category through its own filter toggle', () => {
    const next = applySessionList(createSessionListState(), [
      { id: 'user-session', run_kinds: ['user'] },
      { id: 'cron-session', run_kinds: ['cron'] },
      { id: 'reflection-session', run_kinds: ['reflection'] },
      { id: 'memory-reflection-session', run_kinds: ['memory_reflection'] },
      { id: 'skill-reflection-session', run_kinds: ['skill_reflection'] },
      { id: 'subagent-session', is_subagent_session: true },
    ]);

    const visibleIds = (filters) =>
      visibleSessionsForSelection(next.sessions, { filters }).map(
        (session) => session.id,
      );

    expect(visibleIds({ ...createSessionListFilters(), cron: true })).toEqual([
      'cron-session',
      'user-session',
    ]);
    expect(
      visibleIds({ ...createSessionListFilters(), subagents: true }),
    ).toEqual(['subagent-session', 'user-session']);
    // A combined reflection review covers both dimensions, so either
    // reflection toggle reveals it alongside its specific kind.
    expect(
      visibleIds({ ...createSessionListFilters(), memoryReflections: true }),
    ).toEqual([
      'memory-reflection-session',
      'reflection-session',
      'user-session',
    ]);
    expect(
      visibleIds({ ...createSessionListFilters(), skillReflections: true }),
    ).toEqual([
      'reflection-session',
      'skill-reflection-session',
      'user-session',
    ]);

    const everyFilter = {
      ...createSessionListFilters(),
      subagents: true,
      memoryReflections: true,
      skillReflections: true,
      cron: true,
    };
    expect(visibleIds(everyFilter)).toHaveLength(6);
  });

  it('requires every hiding category before a mixed background session appears', () => {
    const next = applySessionList(createSessionListState(), [
      { id: 'combined-session', run_kinds: ['cron', 'memory_reflection'] },
    ]);

    expect(
      visibleSessionsForSelection(next.sessions, {
        filters: { ...createSessionListFilters(), cron: true },
      }),
    ).toHaveLength(0);
    expect(
      visibleSessionsForSelection(next.sessions, {
        filters: {
          ...createSessionListFilters(),
          cron: true,
          memoryReflections: true,
        },
      }),
    ).toHaveLength(1);
  });

  it('carries the owning agent through normalization for merged lists', () => {
    const next = applySessionList(createSessionListState(), [
      {
        id: 'session-1',
        title: 'Release planning',
        agent_address: 'nabu',
        agent_name: 'Nabu',
      },
      { id: 'session-2', title: 'Untouched' },
    ]);

    expect(next.sessions[0]).toMatchObject({
      agent_address: 'nabu',
      agent_name: 'Nabu',
    });
    expect(next.sessions[1]).toMatchObject({
      agent_address: null,
      agent_name: null,
    });
  });

  it('keeps the selected background session visible in the important view', () => {
    const next = applySessionList(createSessionListState(), [
      { id: 'user-session', run_kinds: ['user'] },
      { id: 'cron-session', run_kinds: ['cron'] },
    ]);

    expect(
      visibleSessionsForSelection(next.sessions, {
        selectedSessionId: 'cron-session',
      }).map((session) => session.id),
    ).toEqual(['cron-session', 'user-session']);
  });

  it('keeps the selected sub-agent session visible in the important view', () => {
    const next = applySessionList(createSessionListState(), [
      { id: 'user-session', run_kinds: ['user'] },
      { id: 'subagent-session', is_subagent_session: true },
    ]);

    expect(
      visibleSessionsForSelection(next.sessions, {
        selectedSessionId: 'subagent-session',
      }).map((session) => session.id),
    ).toEqual(['subagent-session', 'user-session']);
  });
});
