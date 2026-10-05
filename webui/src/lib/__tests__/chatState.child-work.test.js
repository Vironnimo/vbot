import { describe, expect, it, vi } from 'vitest';
import { ApiClientError } from '../api/transport.js';
import { ensureSessionState } from '../chatState.js';
import { subAgentDotStatus } from '../chatTimelinePresentation.js';
import {
  deferred,
  reflectionRun,
  setupController,
} from './chatState.support.js';

describe('Subagent rows', () => {
  it('reconciles a persisted Subagent row through its exact durable work id', async () => {
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-old-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'reused-child',
      run_id: 'old-child-run',
      status: 'completed',
      timing: { duration_ms: 4200 },
    });
    const { chatState, controller } = setupController({
      operationOverrides: { inspectSubAgentWork },
    });
    const tool = {
      type: 'tool_call',
      name: 'subagent',
      status: 'success',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Inspect the old state',
        background: true,
      },
      result: {
        ok: true,
        data: {
          id: 'sub-old-work',
          agent_id: 'worker',
          session_id: 'reused-child',
          run_id: 'old-child-run',
          status: 'running',
          delivery: 'automatic',
        },
      },
    };

    controller.reconcileSubAgentRows(
      [{ type: 'assistant_run', items: [tool] }],
      { projectId: 'project-one' },
    );
    await vi.waitFor(() =>
      expect(chatState.subAgentStatuses).toHaveProperty(
        'run:old-child-run',
        'completed',
      ),
    );

    expect(inspectSubAgentWork).toHaveBeenCalledWith({
      id: 'sub-old-work',
      agent_id: 'worker@project-one',
      session_id: 'reused-child',
    });
    expect(chatState.subAgentStatuses).toMatchObject({
      'run:old-child-run': 'completed',
      'runDuration:old-child-run': 4200,
      'workRun:sub-old-work': 'old-child-run',
    });
  });

  it('keys a run-less Project child inspection by the address the row reads', async () => {
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-project-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'child-session',
      run_id: null,
      status: 'completed',
      timing: { duration_ms: 4200 },
      tool_name: 'read',
    });
    const { chatState, controller } = setupController({
      operationOverrides: { inspectSubAgentWork },
    });
    const tool = {
      type: 'tool_call',
      name: 'subagent',
      status: 'success',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Inspect the project',
        background: true,
      },
      result: {
        ok: true,
        data: {
          id: 'sub-project-work',
          agent_id: 'worker@project-one',
          project_id: 'project-one',
          session_id: 'child-session',
          status: 'running',
          delivery: 'automatic',
        },
      },
    };

    controller.reconcileSubAgentRows(
      [{ type: 'assistant_run', items: [tool] }],
      { projectId: 'project-one' },
    );
    await vi.waitFor(() =>
      expect(chatState.subAgentStatuses).toMatchObject({
        'session:worker@project-one::child-session': 'completed',
      }),
    );

    expect(inspectSubAgentWork).toHaveBeenCalledWith({
      id: 'sub-project-work',
      agent_id: 'worker@project-one',
      session_id: 'child-session',
    });
    expect(chatState.subAgentStatuses).toEqual({
      'session:worker@project-one::child-session': 'completed',
      'sessionDuration:worker@project-one::child-session': 4200,
      'sessionTool:worker@project-one::child-session': 'read',
    });
  });

  it('settles a reloaded Subagent row from live events once inspection found its child Run', async () => {
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-live-work',
      agent_id: 'worker',
      session_id: 'child-session',
      run_id: 'child-run',
      status: 'running',
    });
    const { chatState, controller } = setupController({
      operationOverrides: { inspectSubAgentWork },
    });
    const tool = reloadedRow('sub-live-work');
    const items = [{ type: 'assistant_run', items: [tool] }];

    controller.reconcileSubAgentRows(items);
    await vi.waitFor(() =>
      expect(chatState.subAgentStatuses).toMatchObject({
        'run:child-run': 'running',
        'workRun:sub-live-work': 'child-run',
      }),
    );

    controller.applySubAgentStatusUpdates({ 'run:child-run': 'completed' });
    controller.reconcileSubAgentRows(items);

    expect(subAgentDotStatus(tool, chatState.subAgentStatuses)).toBe('success');
    expect(inspectSubAgentWork).toHaveBeenCalledOnce();
  });

  it.each([
    ['cancels the Run it finds working', 'running', true],
    ['leaves a Sub-Agent with nothing running alone', 'completed', false],
  ])(
    'inspects a Subagent row without a known Run and %s',
    async (_label, status, cancels) => {
      const inspectSubAgentWork = vi.fn().mockResolvedValue({
        id: 'sub-work',
        agent_id: 'worker',
        project_id: 'project-one',
        session_id: 'child-session',
        run_id: 'child-run',
        status,
      });
      const cancelRun = vi.fn().mockResolvedValue({ status: 'cancelled' });
      const { chatState, controller } = setupController({
        operationOverrides: { cancelRun, inspectSubAgentWork },
      });
      const sessionState = ensureSessionState(
        chatState,
        'parent',
        'parent-session',
      );

      await expect(
        controller.cancelSubAgent({
          tool: reloadedRow('sub-work'),
          sessionState,
          projectId: 'project-one',
        }),
      ).resolves.toBe(true);

      expect(inspectSubAgentWork).toHaveBeenCalledWith({
        id: 'sub-work',
        agent_id: 'worker@project-one',
        session_id: 'child-session',
      });
      expect(cancelRun.mock.calls).toEqual(
        cancels ? [['child-run', { reason: 'user' }]] : [],
      );
      expect(chatState.subAgentStatuses).toMatchObject({
        'run:child-run': cancels ? 'cancelled' : 'completed',
        'workRun:sub-work': 'child-run',
      });
      expect(sessionState.actionError).toBe('');
    },
  );
});

describe('Sub-Agent work', () => {
  it('reads what a Sub-Agent still runs through its exact work id', async () => {
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'child-session',
      run_id: 'child-run',
      status: 'completed',
      running: [
        { kind: 'subagent', id: 'sub_inner', label: 'Inner worker' },
        { kind: 'command', id: 'term_1', label: 'npm run dev' },
        { kind: 'unknown', id: 'x', label: 'dropped' },
      ],
    });
    const { chatState, controller } = setupController({
      operationOverrides: { inspectSubAgentWork },
    });

    await expect(
      controller.loadSubAgentWork({
        tool: reloadedRow('sub-work'),
        projectId: 'project-one',
      }),
    ).resolves.toBe(true);

    expect(inspectSubAgentWork).toHaveBeenCalledWith({
      id: 'sub-work',
      agent_id: 'worker@project-one',
      session_id: 'child-session',
    });
    expect(chatState.subAgentWork).toEqual({
      'sub-work': [
        { kind: 'subagent', id: 'sub_inner', label: 'Inner worker' },
        { kind: 'command', id: 'term_1', label: 'npm run dev' },
      ],
    });
    expect(chatState.subAgentStatuses).toMatchObject({
      'run:child-run': 'completed',
      'workRun:sub-work': 'child-run',
    });
  });

  it('leaves a row without a Sub-Agent id uninspected', async () => {
    const inspectSubAgentWork = vi.fn();
    const { chatState, controller } = setupController({
      operationOverrides: { inspectSubAgentWork },
    });
    const legacy = reloadedRow('');
    delete legacy.result.data.id;

    await expect(controller.loadSubAgentWork({ tool: legacy })).resolves.toBe(
      false,
    );

    expect(inspectSubAgentWork).not.toHaveBeenCalled();
    expect(chatState.subAgentWork).toEqual({});
  });
});

// A `run` row restored from History: its result names the Sub-Agent and its
// Session, but not the child Run.
function reloadedRow(workId) {
  return {
    type: 'tool_call',
    name: 'subagent',
    status: 'success',
    arguments: {
      action: 'run',
      agent_id: 'worker',
      content: 'Run in the background',
    },
    result: {
      ok: true,
      data: {
        id: workId,
        agent_id: 'worker',
        session_id: 'child-session',
        status: 'running',
      },
    },
  };
}

const OUTCOME = { memory: 1, skills: 0, undone: false };
const REVIEW_CHANGES = [
  {
    store: 'memory',
    kind: 'added',
    revisions: [1],
    undone: false,
    scope: 'user',
    text: 'Prefers short answers.',
  },
];

// A source Session whose finished review saved one Memory entry.
function reviewedSource(chatState, agentAddress = 'alpha') {
  const source = ensureSessionState(chatState, agentAddress, 'source');
  source.reflectionTasks = {
    'review-one': {
      sessionId: 'review-session',
      runKind: 'memory_reflection',
      status: 'completed',
      startedAt: '2026-09-05T10:00:00Z',
      outcome: OUTCOME,
    },
  };
  return source;
}

const runChanges = (undone) => ({
  agent_id: 'alpha',
  run_id: 'review-one',
  summary: { ...OUTCOME, undone },
  changes: REVIEW_CHANGES.map((change) => ({ ...change, undone })),
});

describe('Reflection reviews', () => {
  it('restores completed reflections on fresh history load without touching another Session', async () => {
    const { chatState, controller } = setupController({
      operationOverrides: {
        loadChatHistory: vi.fn().mockResolvedValue({
          messages: [],
          reflection_runs: [{ ...reflectionRun(), outcome: OUTCOME }],
        }),
      },
    });
    const other = ensureSessionState(chatState, 'alpha', 'other');
    await controller.loadHistoryForSession('alpha', 'source');
    expect(
      chatState.sessions['alpha::source'].reflectionTasks['review-one'],
    ).toEqual({
      sessionId: 'review-session',
      runKind: 'memory_reflection',
      status: 'completed',
      startedAt: '2026-09-05T10:00:00Z',
      outcome: OUTCOME,
    });
    expect(other.reflectionTasks).toEqual({});
  });

  it('recovers a reflection completed while disconnected and deduplicates connection snapshots', async () => {
    const loadReflectionRuns = vi
      .fn()
      .mockResolvedValue({ reflection_runs: [reflectionRun()] });
    const { chatState, controller } = setupController({
      operationOverrides: { loadReflectionRuns },
      isDisplayedSession: (agent, session) =>
        agent === 'alpha@project' && session === 'source',
    });
    const source = ensureSessionState(chatState, 'alpha@project', 'source');
    const snapshot = { active_runs: [], queues: [] };
    controller.applyConnectionSnapshot(snapshot);
    controller.applyConnectionSnapshot(snapshot);
    await vi.waitFor(() =>
      expect(source.reflectionTasks['review-one']?.status).toBe('completed'),
    );
    expect(loadReflectionRuns).toHaveBeenCalledExactlyOnceWith({
      agent_id: 'alpha@project',
      session_id: 'source',
    });
  });

  it('keeps newer live results when a reflection restore response arrives late', async () => {
    const response = deferred();
    const { chatState, controller } = setupController({
      operationOverrides: {
        loadChatHistory: vi.fn().mockReturnValue(response.promise),
      },
    });
    const source = ensureSessionState(chatState, 'alpha', 'source');
    const loading = controller.loadHistoryForSession('alpha', 'source');
    const terminal = {
      sessionId: 'review-session',
      runKind: 'memory_reflection',
      status: 'failed',
      startedAt: '2026-09-05T10:00:00Z',
    };
    source.reflectionTasks = {
      'review-one': terminal,
      'new-review': { ...terminal, status: 'running' },
    };
    response.resolve({
      messages: [],
      reflection_runs: [reflectionRun('running')],
    });
    await loading;
    expect(source.reflectionTasks['review-one']).toBe(terminal);
    expect(source.reflectionTasks['new-review'].status).toBe('running');
  });

  it('ignores an obsolete reconnect response and removes deleted review rows', async () => {
    const response = deferred();
    const { chatState, controller } = setupController({
      operationOverrides: {
        loadReflectionRuns: vi.fn().mockReturnValue(response.promise),
        loadChatHistory: vi
          .fn()
          .mockResolvedValue({ messages: [], reflection_runs: [] }),
      },
      isDisplayedSession: () => true,
    });
    const source = ensureSessionState(chatState, 'alpha', 'source');
    source.reflectionTasks = {
      stale: { sessionId: 'deleted', status: 'completed' },
    };
    controller.applyConnectionSnapshot({ active_runs: [] });
    await controller.loadHistoryForSession('alpha', 'source');
    response.resolve({ reflection_runs: [reflectionRun('running')] });
    await Promise.resolve();
    expect(source.reflectionTasks).toEqual({});
  });
});

describe('Review changes', () => {
  it('loads the changes of one review and undoes them into its row', async () => {
    const loadLearningChanges = vi.fn().mockResolvedValue(runChanges(false));
    const undoLearningChanges = vi.fn().mockResolvedValue(runChanges(true));
    const { chatState, controller } = setupController({
      operationOverrides: { loadLearningChanges, undoLearningChanges },
    });
    const source = reviewedSource(chatState);

    expect(await controller.loadReflectionChanges(source, 'review-one')).toBe(
      true,
    );
    expect(loadLearningChanges).toHaveBeenCalledExactlyOnceWith(
      'alpha',
      'review-one',
    );
    expect(source.reflectionDetails['review-one']).toMatchObject({
      changes: REVIEW_CHANGES,
      loading: false,
      loadError: '',
    });

    expect(await controller.undoReflection(source, 'review-one')).toBe(true);
    expect(undoLearningChanges).toHaveBeenCalledExactlyOnceWith(
      'alpha',
      'review-one',
    );
    // The row now says it was undone, and its list shows what was taken back.
    expect(source.reflectionTasks['review-one'].outcome).toEqual({
      ...OUTCOME,
      undone: true,
    });
    expect(source.reflectionDetails['review-one']).toMatchObject({
      changes: [{ ...REVIEW_CHANGES[0], undone: true }],
      undoing: false,
      undoError: null,
    });

    // Only Identity Agents learn: a Project Session has no review changes.
    const project = reviewedSource(chatState, 'alpha@project');
    expect(await controller.loadReflectionChanges(project, 'review-one')).toBe(
      false,
    );
    expect(await controller.undoReflection(project, 'review-one')).toBe(false);
    expect(loadLearningChanges).toHaveBeenCalledOnce();
    expect(undoLearningChanges).toHaveBeenCalledOnce();
  });

  it('keeps a refused undo with the later change that blocks it until the list reopens', async () => {
    const conflict = {
      store: 'memory',
      revision: 1,
      scope: 'user',
      text: 'Prefers short answers.',
      later: { revision: 2, at: '2026-09-05T10:30:00Z', actor: 'rpc' },
    };
    const refusal = new ApiClientError(
      'learning_undo_conflict',
      'Nothing was undone.',
      { details: { code: 'learning_undo_conflict', data: conflict } },
    );
    const failure = new ApiClientError(
      'domain_error',
      'The undo failed part-way. Undo again to finish.',
    );
    const loadLearningChanges = vi.fn().mockResolvedValue(runChanges(false));
    const undoLearningChanges = vi
      .fn()
      .mockRejectedValueOnce(refusal)
      .mockRejectedValueOnce(failure);
    const { chatState, controller } = setupController({
      operationOverrides: { loadLearningChanges, undoLearningChanges },
    });
    const source = reviewedSource(chatState);

    expect(await controller.undoReflection(source, 'review-one')).toBe(false);

    expect(source.reflectionDetails['review-one']).toMatchObject({
      undoing: false,
      undoError: { conflict, message: 'Nothing was undone.' },
    });
    expect(source.reflectionTasks['review-one'].outcome).toEqual(OUTCOME);
    expect(loadLearningChanges).not.toHaveBeenCalled();

    // A failure while writing may leave part of the review undone, so the
    // review is read again.
    expect(await controller.undoReflection(source, 'review-one')).toBe(false);
    expect(source.reflectionDetails['review-one'].undoError).toEqual({
      conflict: null,
      message: failure.message,
    });
    await vi.waitFor(() =>
      expect(source.reflectionDetails['review-one'].changes).toEqual(
        REVIEW_CHANGES,
      ),
    );
    expect(loadLearningChanges).toHaveBeenCalledExactlyOnceWith(
      'alpha',
      'review-one',
    );
    expect(source.reflectionDetails['review-one'].undoError).toEqual({
      conflict: null,
      message: failure.message,
    });

    // Reopening the list reads the review again; an old refusal no longer
    // describes it.
    expect(await controller.loadReflectionChanges(source, 'review-one')).toBe(
      true,
    );
    expect(source.reflectionDetails['review-one'].undoError).toBeNull();
  });
});

describe('handed-off command statuses', () => {
  it('applies live command statuses without letting a stale running undo an end', () => {
    const { chatState, controller } = setupController();

    controller.applyCommandStatuses({ term_one: 'running' });
    controller.applyCommandStatuses({
      term_one: 'running',
      term_two: 'running',
    });
    controller.applyCommandStatuses({
      term_one: 'completed',
      term_two: 'running',
    });
    // A map that still holds an older `running` changes nothing known to end.
    controller.applyCommandStatuses({
      term_one: 'running',
      term_two: 'failed',
    });

    expect(chatState.commandStatuses).toEqual({
      term_one: 'completed',
      term_two: 'failed',
    });
  });
});
