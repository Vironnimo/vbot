import { afterEach, describe, expect, it, vi } from 'vitest';

import { t } from '../i18n.js';
import {
  applySessionList,
  createSessionListFilters,
  createSessionListState,
  isSessionHiddenByDefault,
  loadSessionListFilters,
  overlayLiveSessionActivity,
  selectSession,
  sessionDisplayName,
  sessionParentReference,
  saveSessionListFilters,
  visibleSessionsForSelection,
} from '../sessionListView.js';
import { sessionActivityFromSnapshot } from '../sessionActivity.js';

describe('Session filter storage', () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([null, '{broken', 'null', '[]', 'true', '42'])(
    'ignores missing or malformed stored filters: %s',
    (stored) => {
      vi.stubGlobal('localStorage', { getItem: () => stored });
      expect(loadSessionListFilters(0)).toBeNull();
    },
  );

  it('accepts only known boolean filters and defaults missing fields', () => {
    const setItem = vi.fn();
    vi.stubGlobal('localStorage', {
      getItem: () =>
        JSON.stringify({
          cron: true,
          channels: true,
          allAgents: 'true',
          extra: true,
        }),
      setItem,
    });
    const expected = {
      ...createSessionListFilters(),
      cron: true,
      channels: true,
    };
    expect(loadSessionListFilters(0)).toEqual(expected);
    saveSessionListFilters(1, { ...expected, extra: true });
    expect(setItem).toHaveBeenCalledWith(
      'vbot.chat.sessionFilters.1',
      JSON.stringify(expected),
    );
  });

  it('keeps filters usable when storage reads or writes fail', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('Storage unavailable');
      },
      setItem: () => {
        throw new Error('Storage full');
      },
    });
    expect(loadSessionListFilters(0)).toBeNull();
    expect(() =>
      saveSessionListFilters(0, createSessionListFilters()),
    ).not.toThrow();
  });
});

describe('sessionListView helpers', () => {
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
        activity_state: 'waiting',
        work_started_at: '2026-05-15T09:58:00+00:00',
        waiting_for: { subagents: 0, commands: 2, results: 0 },
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
      display_name: t('sessions.newSession'),
      is_channel_session: false,
      latest_completion_run_id: 'run-one',
      has_unread_completion: true,
      activity_state: 'idle',
      work_started_at: null,
      unread_run_id: 'run-one',
      unread_run_status: 'completed',
    });
    expect(next.sessions[1]).toMatchObject({
      display_name: 'telegram/12345',
      source_channel_id: 'tg-assistant',
      is_channel_session: true,
      activity_state: 'waiting',
      work_started_at: '2026-05-15T09:58:00+00:00',
      waiting_for: { subagents: 0, commands: 2, results: 0 },
    });
  });

  it('overlays live activity by complete agent address and Session id', () => {
    const sessions = applySessionList(createSessionListState(), [
      { id: 'shared-id' },
      {
        id: 'shared-id',
        agent_address: 'reviewer@project',
        has_unread_completion: false,
      },
      {
        id: 'server-only',
        activity_state: 'working',
        work_started_at: '2026-08-31T11:00:00+00:00',
      },
    ]).sessions;
    const unread = [
      {
        agent_address: 'reviewer@project',
        session_id: 'shared-id',
        has_unread_completion: true,
        latest_completion_run_id: 'run-two',
        unread_run_id: 'run-two',
        unread_run_status: 'completed',
        unread_run_at: '2026-08-31T12:00:00+00:00',
      },
    ];
    const rowOf = (projected, agentAddress) =>
      projected.find(
        (session) =>
          session.id === 'shared-id' && session.agent_address === agentAddress,
      );

    // Before App holds Session activity, rows keep what the server listed.
    const beforeHello = overlayLiveSessionActivity(sessions, unread, 'alpha');
    expect(rowOf(beforeHello, null)).toBe(rowOf(sessions, null));
    expect(rowOf(beforeHello, 'reviewer@project')).toMatchObject({
      has_unread_completion: true,
      unread_run_id: 'run-two',
      activity_state: 'idle',
    });
    expect(
      beforeHello.find((session) => session.id === 'server-only'),
    ).toMatchObject({ activity_state: 'working' });

    // App's Session activity names each row by its full Session key; a row
    // it does not hold is idle.
    const activity = sessionActivityFromSnapshot([
      {
        project_id: null,
        agent_id: 'alpha',
        session_id: 'shared-id',
        state: 'waiting',
        work_started_at: '2026-08-31T11:30:00+00:00',
        waiting_for: { subagents: 2, commands: 1, results: 0 },
      },
    ]);
    const projected = overlayLiveSessionActivity(
      sessions,
      unread,
      'alpha',
      activity,
    );
    expect(rowOf(projected, null)).toMatchObject({
      activity_state: 'waiting',
      work_started_at: '2026-08-31T11:30:00+00:00',
      waiting_for: { subagents: 2, commands: 1, results: 0 },
    });
    expect(rowOf(projected, 'reviewer@project')).toMatchObject({
      activity_state: 'idle',
      has_unread_completion: true,
    });
    expect(
      projected.find((session) => session.id === 'server-only'),
    ).toMatchObject({ activity_state: 'idle', work_started_at: null });
    // A row whose live activity is unchanged keeps its identity.
    expect(
      rowOf(overlayLiveSessionActivity(projected, [], 'alpha', activity), null),
    ).toBe(rowOf(projected, null));
  });

  it.each([
    [
      'sub-agent metadata',
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
      {
        is_subagent_session: true,
        subagent_parent: {
          agent_id: 'orchestrator',
          session_id: 'parent-session',
          run_id: 'parent-run',
          tool_call_id: 'tool-call-one',
          tool_call_index: 2,
        },
      },
    ],
    [
      'a fork_source object as a fork',
      {
        id: 'fork-session',
        fork_source: {
          agent_id: 'coder',
          session_id: 'source-session',
          project_id: null,
          forked_at: '2026-07-04T00:00:00+00:00',
        },
      },
      {
        is_fork: true,
        fork_source: {
          agent_id: 'coder',
          session_id: 'source-session',
          project_id: null,
          forked_at: '2026-07-04T00:00:00+00:00',
        },
      },
    ],
    [
      'an absent fork_source as no fork',
      { id: 'plain-session' },
      { is_fork: false, fork_source: null },
    ],
    [
      'a non-object fork_source as no fork',
      { id: 'bad-session', fork_source: 'nope' },
      { is_fork: false, fork_source: null },
    ],
    [
      'an incomplete fork_source as no fork',
      { id: 'bad-fork', fork_source: { agent_id: 'coder' } },
      { is_fork: false, fork_source: null },
    ],
    [
      'the title into the display name',
      {
        id: 'session-1',
        title: 'Release planning',
        platform: 'telegram',
        platform_conv_id: '999',
      },
      { title: 'Release planning', display_name: 'Release planning' },
    ],
    [
      'the owning Agent for merged lists',
      { id: 'session-1', agent_address: 'nabu', agent_name: 'Nabu' },
      { agent_address: 'nabu', agent_name: 'Nabu' },
    ],
    [
      'a missing owning Agent as null',
      { id: 'session-2', title: 'Untouched' },
      { agent_address: null, agent_name: null },
    ],
  ])('normalizes %s', (_label, raw, expected) => {
    const next = applySessionList(createSessionListState(), [raw]);

    expect(next.sessions[0]).toMatchObject({ id: raw.id, ...expected });
  });

  it.each([
    [
      'a Subagent Session',
      {
        subagent_parent: {
          agent_id: 'orchestrator',
          session_id: 'parent-session',
          project_id: 'vbot',
        },
      },
      {
        kind: 'subagent',
        agent_id: 'orchestrator',
        session_id: 'parent-session',
        project_id: 'vbot',
      },
    ],
    ...[[], ['reflection']].map((runKinds) => [
      `a Fork with run kinds [${runKinds}]`,
      {
        run_kinds: runKinds,
        fork_source: {
          agent_id: 'coder',
          session_id: 'source-session',
          project_id: null,
        },
      },
      {
        kind: 'fork',
        agent_id: 'coder',
        session_id: 'source-session',
        project_id: null,
      },
    ]),
    ['an empty subagent_parent', { subagent_parent: {} }, null],
    ['an incomplete fork_source', { fork_source: { agent_id: 'coder' } }, null],
  ])('resolves the immediate parent of %s', (_label, session, parent) => {
    expect(sessionParentReference(session)).toEqual(parent);
  });

  it('clears the selected Session when the list no longer contains it', () => {
    const next = applySessionList(
      { ...createSessionListState(), selectedSessionId: 'missing-session' },
      [{ id: 'known-session' }],
    );

    expect(next.selectedSessionId).toBeNull();
    expect(next.selectedAgentAddress).toBeNull();
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

  it('keeps duplicate Session ids distinct by owning Agent address', () => {
    const state = applySessionList(createSessionListState(), [
      { id: 'shared', agent_address: 'alpha' },
      { id: 'shared', agent_address: 'beta' },
    ]);

    const selected = selectSession(state, 'shared', 'beta');

    expect(selected.selectedSessionId).toBe('shared');
    expect(selected.selectedAgentAddress).toBe('beta');
  });

  const newSession = () => t('sessions.newSession');

  it.each([
    [
      'a channel Session',
      { platform: 'telegram', platform_conv_id: '-100123' },
      () => 'telegram/-100123',
    ],
    // A Session without title, automatic title, or channel identity has no
    // content yet: it shows the neutral label, never its raw id.
    ['a Session with only an id', { id: 'session-001' }, newSession],
    ['an empty Session', {}, newSession],
    [
      'a user title over channel and id',
      {
        title: 'Release planning',
        platform: 'telegram',
        platform_conv_id: '-100123',
        id: 'session-001',
      },
      () => 'Release planning',
    ],
    [
      'a blank title',
      { title: '   ', platform: 'telegram', platform_conv_id: '-100123' },
      () => 'telegram/-100123',
    ],
    [
      'an automatic title',
      { auto_title: 'Generated title', id: 'session-001' },
      () => 'Generated title',
    ],
    [
      'a manual title over the automatic title',
      {
        title: 'Manual title',
        auto_title: 'Generated title',
        id: 'session-001',
      },
      () => 'Manual title',
    ],
  ])('names %s', (_label, session, name) => {
    expect(sessionDisplayName(session)).toBe(name());
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
      // A Cron Run that reported to the user lists its Session.
      {
        id: 'reported-cron-session',
        run_kinds: ['cron'],
        latest_completion_run_id: 'run-1',
      },
      // The server lists a Librarian Session only in the Librarian's own
      // scope, where it is an ordinary conversation.
      { id: 'librarian-session', run_kinds: ['librarian'] },
      {
        id: 'channel-session',
        run_kinds: ['cron'],
        platform: 'telegram',
        platform_conv_id: '12345',
      },
    ]);

    expect(
      visibleSessionsForSelection(next.sessions).map((session) => session.id),
    ).toEqual([
      'librarian-session',
      'mixed-session',
      'reported-cron-session',
      'user-session',
    ]);

    expect(
      visibleSessionsForSelection(next.sessions, {
        filters: createSessionListFilters(),
      }).map((session) => session.id),
    ).toEqual([
      'librarian-session',
      'mixed-session',
      'reported-cron-session',
      'user-session',
    ]);

    expect(
      isSessionHiddenByDefault(
        next.sessions.find((session) => session.id === 'subagent-session'),
      ),
    ).toBe(true);
  });

  it('reveals channels independently and retains the selected channel', () => {
    const { sessions } = applySessionList(createSessionListState(), [
      { id: 'ordinary' },
      {
        id: 'telegram',
        platform: 'telegram',
        platform_conv_id: '1',
        run_kinds: ['cron'],
      },
      { id: 'discord', platform: 'discord', platform_conv_id: '2' },
    ]);
    const ids = (options) =>
      visibleSessionsForSelection(sessions, options).map((row) => row.id);
    expect(ids()).toEqual(['ordinary']);
    expect(ids({ filters: { channels: true } })).toEqual([
      'discord',
      'ordinary',
      'telegram',
    ]);
    expect(ids({ filters: { cron: true } })).toEqual(['ordinary']);
    expect(ids({ selectedSessionId: 'telegram' })).toEqual([
      'ordinary',
      'telegram',
    ]);
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

  it.each([
    ['background', { id: 'cron-session', run_kinds: ['cron'] }],
    ['sub-agent', { id: 'subagent-session', is_subagent_session: true }],
  ])(
    'keeps the selected %s Session visible in the important view',
    (_label, hidden) => {
      const next = applySessionList(createSessionListState(), [
        { id: 'user-session', run_kinds: ['user'] },
        hidden,
      ]);

      expect(
        visibleSessionsForSelection(next.sessions, {
          selectedSessionId: hidden.id,
        }).map((session) => session.id),
      ).toEqual([hidden.id, 'user-session']);
    },
  );
});
