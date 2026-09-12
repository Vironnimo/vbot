import { describe, expect, it, vi } from 'vitest';
import { ensureSessionState } from '../chatState.js';
import { setup } from './chatState.controller.support.js';

describe('chat controller', () => {
  it('reconciles a persisted Subagent row through its exact durable work id', async () => {
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-old-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'reused-child',
      run_id: 'old-child-run',
      status: 'completed',
      result: 'Exact older result',
      timing: { duration_ms: 4200 },
    });
    const { chatState, controller } = setup({
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
      expect(chatState.subAgentResults['work:sub-old-work']).toMatchObject({
        loading: false,
        result: 'Exact older result',
      }),
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

  it('settles a live Subagent row after inspection discovers its child Run', async () => {
    const inspectSubAgentWork = vi
      .fn()
      .mockResolvedValueOnce({
        id: 'sub-live-work',
        agent_id: 'worker',
        session_id: 'child-session',
        run_id: 'child-run',
        status: 'running',
        result: null,
      })
      .mockResolvedValueOnce({
        id: 'sub-live-work',
        agent_id: 'worker',
        session_id: 'child-session',
        run_id: 'child-run',
        status: 'completed',
        result: 'Live child result',
      });
    const { chatState, controller } = setup({
      operationOverrides: { inspectSubAgentWork },
    });
    const tool = {
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
          id: 'sub-live-work',
          agent_id: 'worker',
          session_id: 'child-session',
          status: 'running',
          delivery: 'automatic',
        },
      },
    };
    const items = [{ type: 'assistant_run', items: [tool] }];

    controller.reconcileSubAgentRows(items);
    await vi.waitFor(() =>
      expect(chatState.subAgentStatuses).toMatchObject({
        'run:child-run': 'running',
        'workRun:sub-live-work': 'child-run',
      }),
    );

    controller.applySubAgentStatusUpdates({
      'run:child-run': 'completed',
    });
    controller.reconcileSubAgentRows(items);

    await vi.waitFor(() =>
      expect(chatState.subAgentResults['work:sub-live-work']).toMatchObject({
        loading: false,
        result: 'Live child result',
      }),
    );
    expect(inspectSubAgentWork).toHaveBeenCalledTimes(2);
  });

  it('cancels a consumed queued Subagent through exact work inspection', async () => {
    const removeFromQueue = vi.fn().mockRejectedValue(
      Object.assign(new Error('already started'), {
        code: 'queue_item_not_found',
      }),
    );
    const inspectSubAgentWork = vi.fn().mockResolvedValue({
      id: 'sub-queued-work',
      agent_id: 'worker',
      project_id: 'project-one',
      session_id: 'child-session',
      run_id: 'admitted-child-run',
      status: 'running',
      result: null,
    });
    const cancelRun = vi.fn().mockResolvedValue({ status: 'cancelled' });
    const { chatState, controller } = setup({
      operationOverrides: {
        cancelRun,
        inspectSubAgentWork,
        removeFromQueue,
      },
    });
    const sessionState = ensureSessionState(
      chatState,
      'parent',
      'parent-session',
    );
    const tool = {
      type: 'tool_call',
      name: 'subagent',
      status: 'success',
      arguments: {
        action: 'run',
        agent_id: 'worker',
        content: 'Queued work',
      },
      result: {
        ok: true,
        data: {
          id: 'sub-queued-work',
          agent_id: 'worker',
          session_id: 'child-session',
          queue_item_id: 'queue-item-one',
          status: 'queued',
        },
      },
    };

    await expect(
      controller.cancelSubAgent({
        tool,
        sessionState,
        projectId: 'project-one',
      }),
    ).resolves.toBe(true);

    expect(inspectSubAgentWork).toHaveBeenCalledWith({
      id: 'sub-queued-work',
      agent_id: 'worker@project-one',
      session_id: 'child-session',
    });
    expect(cancelRun).toHaveBeenCalledWith('admitted-child-run', {
      reason: 'user',
    });
    expect(chatState.subAgentStatuses).toMatchObject({
      'run:admitted-child-run': 'cancelled',
      'queue:queue-item-one': 'cancelled',
      'queueRun:queue-item-one': 'admitted-child-run',
    });
  });

  it('merges background Bash status events into the bounded process map', () => {
    const { chatState, controller } = setup();

    controller.applyBackgroundBashStatusEvents([
      {
        type: 'bash_process_status_changed',
        payload: {
          process_id: 'process-one',
          status: 'completed',
          exit_code: 0,
          cancelled_by_user: false,
          started_at: '2026-09-04T12:00:00Z',
          finished_at: '2026-09-04T12:04:12Z',
          output: 'build finished',
          truncated: false,
          log_file: 'C:/logs/bash/process-one.log',
        },
      },
    ]);
    controller.applyBackgroundBashStatusEvents([
      {
        type: 'bash_process_status_changed',
        payload: { process_id: 'process-two', status: 'failed', exit_code: 1 },
      },
    ]);
    // Re-applying the same event is idempotent.
    controller.applyBackgroundBashStatusEvents([
      {
        type: 'bash_process_status_changed',
        payload: { process_id: 'process-two', status: 'failed', exit_code: 1 },
      },
    ]);

    expect(chatState.backgroundBashProcesses['process-one']).toEqual({
      status: 'completed',
      exitCode: 0,
      cancelledByUser: false,
      startedAt: '2026-09-04T12:00:00Z',
      finishedAt: '2026-09-04T12:04:12Z',
      output: 'build finished',
      truncated: false,
      logFile: 'C:/logs/bash/process-one.log',
    });
    expect(chatState.backgroundBashProcesses['process-two']).toEqual({
      status: 'failed',
      exitCode: 1,
      cancelledByUser: false,
      startedAt: '',
      finishedAt: '',
      output: '',
      truncated: false,
      logFile: '',
    });
    expect(Object.keys(chatState.backgroundBashProcesses)).toHaveLength(2);
  });

  it('ignores background Bash status events without a process id', () => {
    const { chatState, controller } = setup();

    controller.applyBackgroundBashStatusEvents([
      { type: 'bash_process_status_changed', payload: { status: 'completed' } },
    ]);
    controller.applyBackgroundBashStatusEvents([]);

    expect(chatState.backgroundBashProcesses).toEqual({});
  });
});
