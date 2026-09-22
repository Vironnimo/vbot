import { describe, expect, it, vi } from 'vitest';
import { ensureSessionState } from '../chatState.js';
import { deferred, setup } from './chatState.controller.support.js';

describe('chat controller', () => {
  it.each(['resolve', 'reject'])(
    'ignores an older roster %s after a silent refresh completes initial loading',
    async (outcome) => {
      const older = deferred();
      const newer = deferred();
      const loadChatHistory = vi
        .fn()
        .mockResolvedValue({ messages: [], active_run: null });
      const { chatState, controller, onAgentsChanged, onAgentSelected } = setup(
        {
          operationOverrides: {
            listAgents: vi
              .fn()
              .mockReturnValueOnce(older.promise)
              .mockReturnValueOnce(newer.promise),
            loadChatHistory,
          },
        },
      );
      const initial = controller.loadAgents();
      const refresh = controller.loadAgents({ silent: true });
      expect(chatState.loadingAgents).toBe(true);
      const agents = [{ id: 'beta', current_session_id: 'beta-session' }];
      newer.resolve({ agents });
      expect(await refresh).toBe(true);
      expect(chatState.loadingAgents).toBe(false);
      expect(loadChatHistory).toHaveBeenCalledWith(
        expect.objectContaining({
          agent_id: 'beta',
          session_id: 'beta-session',
        }),
      );
      if (outcome === 'resolve') older.resolve({ agents: [{ id: 'deleted' }] });
      else older.reject(new Error('stale failure'));
      expect(await initial).toBe(false);
      expect(chatState.agents).toEqual(agents);
      expect(chatState.selectedAgentId).toBe('beta');
      expect(chatState.agentsError).toBeNull();
      expect(onAgentsChanged).toHaveBeenCalledExactlyOnceWith(agents);
      expect(onAgentSelected).toHaveBeenCalledExactlyOnceWith('beta');
      expect(loadChatHistory).toHaveBeenCalledOnce();
    },
  );

  it('does not clear a newer roster loading state when an older request finishes', async () => {
    const older = deferred();
    const newer = deferred();
    const { chatState, controller } = setup({
      operationOverrides: {
        listAgents: vi
          .fn()
          .mockReturnValueOnce(older.promise)
          .mockReturnValueOnce(newer.promise),
      },
    });
    const initial = controller.loadAgents();
    const refresh = controller.loadAgents({ silent: true });
    older.resolve({ agents: [{ id: 'deleted' }] });
    expect(await initial).toBe(false);
    expect(chatState.loadingAgents).toBe(true);
    newer.resolve({ agents: [] });
    await refresh;
    expect(chatState.loadingAgents).toBe(false);
  });

  it.each(['resolve', 'reject'])(
    'ignores pending roster %s after disposal',
    async (outcome) => {
      const response = deferred();
      const { chatState, controller, onAgentsChanged, onAgentSelected } = setup(
        {
          operationOverrides: { listAgents: vi.fn(() => response.promise) },
        },
      );
      const loading = controller.loadAgents();
      controller.destroy();
      if (outcome === 'resolve')
        response.resolve({ agents: [{ id: 'alpha' }] });
      else response.reject(new Error('late failure'));
      expect(await loading).toBe(false);
      expect(chatState.agents).toEqual([]);
      expect(chatState.loadingAgents).toBe(false);
      expect(chatState.agentsError).toBeNull();
      expect(onAgentsChanged).not.toHaveBeenCalled();
      expect(onAgentSelected).not.toHaveBeenCalled();
    },
  );
  it('loads the roster, current history, Run truth, and Queue as one lifecycle', async () => {
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      has_more: false,
      messages: [{ id: 'message-one', role: 'user', content: 'Hello' }],
    });
    const listAgents = vi.fn().mockResolvedValue({
      agents: [
        {
          id: 'alpha',
          name: 'Alpha',
          current_session_id: 'session-one',
        },
      ],
    });
    const { chatState, controller, listQueue, runStream } = setup({
      isDisplayedSession: (agentId, sessionId) =>
        agentId === 'alpha' && sessionId === 'session-one',
      operationOverrides: { listAgents, loadChatHistory },
    });

    await controller.loadAgents();

    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    expect(chatState.selectedAgentId).toBe('alpha');
    expect(sessionState.messages).toMatchObject([
      { id: 'message-one', content: 'Hello' },
    ]);
    expect(listQueue).toHaveBeenCalledWith('alpha', 'session-one');
    expect(runStream.attachRunStream).toHaveBeenCalledWith(sessionState, null);
    expect(chatState.loadingHistory).toBe(false);
  });

  it('stops the Agent loading state before current History settles', async () => {
    let resolveHistory;
    const loadChatHistory = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
    );
    const listAgents = vi.fn().mockResolvedValue({
      agents: [
        {
          id: 'alpha',
          name: 'Alpha',
          current_session_id: 'session-one',
        },
      ],
    });
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { listAgents, loadChatHistory },
    });

    const loading = controller.loadAgents();
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());

    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.loadingHistory).toBe(true);

    resolveHistory({ active_run: null, messages: [], has_more: false });
    await expect(loading).resolves.toBe(true);
    expect(chatState.loadingHistory).toBe(false);
  });

  it('silent loadAgents skips loadingAgents flag and history reload', async () => {
    const loadChatHistory = vi.fn().mockResolvedValue({
      active_run: null,
      messages: [],
      has_more: false,
    });
    const listAgents = vi.fn().mockResolvedValue({
      agents: [
        {
          id: 'alpha',
          name: 'Alpha',
          current_session_id: 'session-one',
        },
      ],
    });
    const { chatState, controller } = setup({
      isDisplayedSession: () => true,
      operationOverrides: { listAgents, loadChatHistory },
    });

    await controller.loadAgents({ silent: true });

    expect(chatState.loadingAgents).toBe(false);
    expect(chatState.agents).toHaveLength(1);
    expect(loadChatHistory).not.toHaveBeenCalled();
  });

  it.each(['response', 'error'])(
    'ignores a stale roster %s after a newer refresh loads the current Session',
    async (outcome) => {
      const old = deferred();
      const loadChatHistory = vi.fn().mockResolvedValue({ messages: [] });
      const { chatState, controller } = setup({
        isDisplayedSession: () => true,
        operationOverrides: {
          listAgents: vi
            .fn()
            .mockReturnValueOnce(old.promise)
            .mockResolvedValueOnce({
              agents: [
                { id: 'alpha', name: 'Renamed', current_session_id: 'new' },
              ],
            }),
          loadChatHistory,
        },
      });
      const initial = controller.loadAgents();
      await controller.loadAgents({ silent: true });
      expect(chatState.loadingAgents).toBe(false);
      expect(loadChatHistory).toHaveBeenCalledWith(
        expect.objectContaining({ session_id: 'new' }),
      );
      if (outcome === 'error') old.reject(new Error('outdated failure'));
      else
        old.resolve({
          agents: [{ id: 'alpha', name: 'Old', current_session_id: 'old' }],
        });
      await initial;
      expect(chatState.agents).toMatchObject([
        { name: 'Renamed', current_session_id: 'new' },
      ]);
      expect(chatState.agentsError).toBeNull();
      expect(loadChatHistory).toHaveBeenCalledOnce();
    },
  );

  it('retires a roster request when its Chat controller is destroyed', async () => {
    const response = deferred();
    const { chatState, controller } = setup({
      operationOverrides: { listAgents: vi.fn(() => response.promise) },
    });
    const pending = controller.loadAgents();
    controller.destroy();
    response.resolve({
      agents: [{ id: 'obsolete', current_session_id: 'old' }],
    });
    await expect(pending).resolves.toBe(false);
    expect(chatState.agents).toEqual([]);
    expect(chatState.loadingAgents).toBe(false);
  });

  it('normalizes command suggestions inside the controller', async () => {
    const listChatCommands = vi.fn().mockResolvedValue({
      items: [
        { name: '/HELP', type: 'command', description: 'Show help' },
        { name: 'review', type: 'skill', description: 'Review code' },
      ],
    });
    const { chatState, controller } = setup({
      operationOverrides: { listChatCommands },
    });

    await controller.loadCommands('alpha');

    expect(listChatCommands).toHaveBeenCalledWith({ agent_id: 'alpha' });
    expect(chatState.availableSkills).toMatchObject([
      { name: 'help', type: 'command' },
      { name: 'review', type: 'skill' },
    ]);
  });

  it('ignores stale command errors after a newer Agent catalog loads', async () => {
    let rejectOlderRequest;
    const listChatCommands = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectOlderRequest = reject;
          }),
      )
      .mockResolvedValueOnce({
        items: [{ name: 'review', type: 'skill', description: 'Review code' }],
      });
    const { chatState, controller } = setup({
      operationOverrides: { listChatCommands },
    });

    const olderLoad = controller.loadCommands('alpha');
    expect(await controller.loadCommands('beta')).toBe(true);
    rejectOlderRequest(new Error('old Agent offline'));
    expect(await olderLoad).toBe(false);

    expect(chatState.commandsError).toBe('');
    expect(chatState.availableSkills).toMatchObject([
      { name: 'review', type: 'skill' },
    ]);
  });

  it('refreshes durable completion activity for every listed Agent Session', async () => {
    const listSessionActivity = vi.fn(async () => ({
      agents: [
        {
          agent_id: 'alpha',
          project_id: null,
          sessions: [
            {
              id: 'session-one',
              has_unread_completion: true,
              unread_run_id: 'run-one',
              unread_run_status: 'completed',
              unread_run_at: '2026-07-20T10:00:00+00:00',
            },
          ],
        },
        { agent_id: 'beta', project_id: null, sessions: [] },
      ],
    }));
    const { chatState, controller } = setup({
      operationOverrides: { listSessionActivity },
    });

    await controller.refreshAgentActivity(['alpha', 'beta', 'alpha']);

    expect(listSessionActivity).toHaveBeenCalledOnce();
    expect(listSessionActivity).toHaveBeenCalledWith(['alpha', 'beta']);
    expect(chatState.loadingAgentActivity).toBe(false);
    expect(chatState.agentActivityError).toBe('');
    expect(ensureSessionState(chatState, 'alpha', 'session-one')).toMatchObject(
      {
        hasUnreadCompletion: true,
        unreadRunId: 'run-one',
        unreadRunStatus: 'completed',
      },
    );
  });

  it('keeps Project Agent activity scoped to its qualified address', async () => {
    const listSessionActivity = vi.fn(async () => ({
      agents: [
        {
          agent_id: 'builder',
          project_id: 'project-one',
          sessions: [
            {
              id: 'project-session',
              has_unread_completion: true,
              unread_run_id: 'project-run',
              unread_run_status: 'failed',
              unread_run_at: '2026-07-20T11:00:00+00:00',
            },
          ],
        },
      ],
    }));
    const { chatState, controller } = setup({
      operationOverrides: { listSessionActivity },
    });

    await controller.refreshAgentActivity(['builder@project-one']);

    expect(listSessionActivity).toHaveBeenCalledWith(['builder@project-one']);
    expect(
      ensureSessionState(chatState, 'builder@project-one', 'project-session'),
    ).toMatchObject({
      hasUnreadCompletion: true,
      unreadRunId: 'project-run',
      unreadRunStatus: 'failed',
    });
    expect(chatState.sessions['builder::project-session']).toBeUndefined();
  });

  it('acknowledges only the exact completion rendered in the Session', async () => {
    const markSessionRead = vi.fn().mockResolvedValue({
      marked_read: true,
      latest_completion_run_id: 'run-one',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });
    const { chatState, controller } = setup({
      operationOverrides: { markSessionRead },
    });
    const sessionState = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';

    expect(await controller.markSessionCompletionRead(sessionState)).toBe(true);

    expect(markSessionRead).toHaveBeenCalledWith(
      'builder@project-one',
      'session-one',
      'run-one',
    );
    expect(sessionState.hasUnreadCompletion).toBe(false);
    expect(sessionState.unreadRunId).toBe('');
  });

  it('does not let a stale Session list resurrect an acknowledged completion', async () => {
    let resolveList;
    const listSessionActivity = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveList = resolve;
        }),
    );
    const markSessionRead = vi.fn().mockResolvedValue({
      marked_read: true,
      latest_completion_run_id: 'run-one',
      has_unread_completion: false,
      unread_run_id: null,
      unread_run_status: null,
      unread_run_at: null,
    });
    const { chatState, controller } = setup({
      operationOverrides: { listSessionActivity, markSessionRead },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';
    const refresh = controller.refreshAgentActivity(['alpha']);

    await controller.markSessionCompletionRead(sessionState);
    resolveList({
      agents: [
        {
          agent_id: 'alpha',
          project_id: null,
          sessions: [
            {
              id: 'session-one',
              has_unread_completion: true,
              unread_run_id: 'run-one',
              unread_run_status: 'completed',
              unread_run_at: '2026-07-20T10:00:00+00:00',
            },
          ],
        },
      ],
    });

    expect(await refresh).toBe(false);
    expect(sessionState.hasUnreadCompletion).toBe(false);
  });

  it('retains known activity when the batched refresh fails', async () => {
    const listSessionActivity = vi
      .fn()
      .mockRejectedValue(new Error('activity unavailable'));
    const { chatState, controller } = setup({
      operationOverrides: { listSessionActivity },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.latestCompletionRunId = 'run-one';
    sessionState.hasUnreadCompletion = true;
    sessionState.unreadRunId = 'run-one';

    await expect(controller.refreshAgentActivity(['alpha'])).resolves.toBe(
      false,
    );

    expect(sessionState.hasUnreadCompletion).toBe(true);
    expect(sessionState.unreadRunId).toBe('run-one');
    expect(chatState.loadingAgentActivity).toBe(false);
    expect(chatState.agentActivityError).toBe('activity unavailable');
  });
});
