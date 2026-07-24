import { describe, expect, it } from 'vitest';

import {
  formatAgentAddress,
  isProjectSelected,
  pickProjectAgentSessionId,
  resolveAgentAddressing,
  resolveMoveActionFromResponse,
  resolveMoveTarget,
} from '../chatState.js';

describe('project chat addressing helpers (RPC-contract traps)', () => {
  it('formats the outside agent address: bare id for identity, agent@projekt for a project', () => {
    // No project → bare id, byte-identical to today's identity payloads.
    expect(formatAgentAddress('builder', '')).toBe('builder');
    expect(formatAgentAddress('builder', null)).toBe('builder');
    expect(formatAgentAddress('builder', undefined)).toBe('builder');
    // A project → the qualified address.
    expect(formatAgentAddress('builder', 'vbot')).toBe('builder@vbot');
    // Whitespace-only project id is treated as no project.
    expect(formatAgentAddress('builder', '   ')).toBe('builder');
  });

  it('distinguishes a real project selection from Personal/empty', () => {
    expect(isProjectSelected('vbot')).toBe(true);
    expect(isProjectSelected('')).toBe(false);
    expect(isProjectSelected('   ')).toBe(false);
    expect(isProjectSelected(null)).toBe(false);
    expect(isProjectSelected(undefined)).toBe(false);
  });

  it('resolves identity addressing so agentAddress equals the bare id (no regression)', () => {
    // Identity agent: projectId null, address === bare id. Every RPC that
    // takes an agent address therefore sends exactly today's value.
    const identity = resolveAgentAddressing('builder', '', false);
    expect(identity).toEqual({
      bareAgentId: 'builder',
      projectId: null,
      agentAddress: 'builder',
    });

    // Even if isProjectAgent is true, an empty project id stays identity —
    // a Personal selection never produces a project address.
    const personal = resolveAgentAddressing('builder', '', true);
    expect(personal.agentAddress).toBe('builder');
    expect(personal.projectId).toBeNull();
  });

  it('resolves project addressing: full address for chat, bare id retained separately (trap 2)', () => {
    const projectAddressing = resolveAgentAddressing('builder', 'vbot', true);
    expect(projectAddressing).toEqual({
      bareAgentId: 'builder',
      projectId: 'vbot',
      agentAddress: 'builder@vbot',
    });
    // The full address goes to chat/session/history; the bare id goes to
    // queue/cancel-tool. Both are available from one resolution.
    expect(projectAddressing.agentAddress).toBe('builder@vbot');
    expect(projectAddressing.bareAgentId).toBe('builder');
  });

  it('picks the most recently active session for a project agent (trap 1)', () => {
    // A project (config) agent has no server current_session_id; the accessor
    // chooses the newest from session.list by last_active_at.
    const sessions = [
      {
        id: 'old',
        created_at: '2026-06-01T00:00:00+00:00',
        last_active_at: '2026-06-01T08:00:00+00:00',
      },
      {
        id: 'newest',
        created_at: '2026-06-02T00:00:00+00:00',
        last_active_at: '2026-06-10T09:00:00+00:00',
      },
      {
        id: 'middle',
        created_at: '2026-06-03T00:00:00+00:00',
        last_active_at: '2026-06-05T09:00:00+00:00',
      },
    ];
    expect(pickProjectAgentSessionId(sessions)).toBe('newest');
  });

  it('falls back to created_at when last_active_at is missing, and returns empty for no sessions', () => {
    const sessions = [
      { id: 'first', created_at: '2026-06-01T00:00:00+00:00' },
      { id: 'second', created_at: '2026-06-09T00:00:00+00:00' },
    ];
    expect(pickProjectAgentSessionId(sessions)).toBe('second');

    // Empty list → '' (the caller then creates a new session via
    // session.create).
    expect(pickProjectAgentSessionId([])).toBe('');
    expect(pickProjectAgentSessionId(null)).toBe('');
    expect(pickProjectAgentSessionId([{ created_at: 'x' }])).toBe('');
  });

  it('does not use sub-agent sessions as the Agent-bar landing session', () => {
    const sessions = [
      {
        id: 'user-session',
        last_active_at: '2026-07-20T09:00:00+00:00',
      },
      {
        id: 'newer-child',
        last_active_at: '2026-07-20T10:00:00+00:00',
        is_subagent_session: true,
        subagent_parent: {
          agent_id: 'orchestrator',
          session_id: 'parent-session',
        },
      },
    ];

    expect(pickProjectAgentSessionId(sessions)).toBe('user-session');
    expect(pickProjectAgentSessionId([sessions[1]])).toBe('');
  });
});

describe('resolveMoveTarget (/agent move-action routing decision)', () => {
  it('routes a bare-id target to the identity world', () => {
    expect(resolveMoveTarget('assistant')).toEqual({
      isProjectTarget: false,
      world: 'identity',
      bareAgentId: 'assistant',
      projectId: null,
      agentAddress: 'assistant',
    });
  });

  it('routes an agent@projekt target to the project world', () => {
    expect(resolveMoveTarget('builder@vbot')).toEqual({
      isProjectTarget: true,
      world: 'project',
      bareAgentId: 'builder',
      projectId: 'vbot',
      agentAddress: 'builder@vbot',
    });
  });

  it('treats a malformed address defensively as identity (lossless split)', () => {
    // Empty parts around the `@` are not a valid project address; the client
    // splitter falls back to identity (the server is the real validator).
    expect(resolveMoveTarget('@vbot').world).toBe('identity');
    expect(resolveMoveTarget('builder@').world).toBe('identity');
    expect(resolveMoveTarget('a@b@c').world).toBe('identity');
  });
});

describe('resolveMoveActionFromResponse (all four move directions)', () => {
  const moveResponse = (agentId) => ({
    command_handled: true,
    reply: 'Moved.',
    output: 'action',
    data: { command: 'agent', session_id: 'session-keep', agent_id: agentId },
  });

  it('decodes an identity target (identity → identity, project → identity)', () => {
    expect(resolveMoveActionFromResponse(moveResponse('assistant'))).toEqual({
      isProjectTarget: false,
      world: 'identity',
      bareAgentId: 'assistant',
      projectId: null,
      agentAddress: 'assistant',
      sessionId: 'session-keep',
    });
  });

  it('decodes a project target (identity → project, project → project)', () => {
    expect(resolveMoveActionFromResponse(moveResponse('builder@vbot'))).toEqual(
      {
        isProjectTarget: true,
        world: 'project',
        bareAgentId: 'builder',
        projectId: 'vbot',
        agentAddress: 'builder@vbot',
        sessionId: 'session-keep',
      },
    );
  });

  it('keeps the SAME session id (a move never invents a new session)', () => {
    const action = resolveMoveActionFromResponse(moveResponse('reviewer@vbot'));
    expect(action.sessionId).toBe('session-keep');
  });

  it('returns null for non-move command responses', () => {
    expect(
      resolveMoveActionFromResponse({
        data: { command: 'handoff', session_id: 's', agent_id: 'beta' },
      }),
    ).toBeNull();
    expect(
      resolveMoveActionFromResponse({
        data: { command: 'new', session_id: 's' },
      }),
    ).toBeNull();
    expect(resolveMoveActionFromResponse({ output: 'transient' })).toBeNull();
    expect(resolveMoveActionFromResponse(null)).toBeNull();
  });

  it('returns null when the move is missing its session or target', () => {
    expect(
      resolveMoveActionFromResponse({
        data: { command: 'agent', agent_id: 'beta' },
      }),
    ).toBeNull();
    expect(
      resolveMoveActionFromResponse({
        data: { command: 'agent', session_id: 's', agent_id: '' },
      }),
    ).toBeNull();
  });
});
