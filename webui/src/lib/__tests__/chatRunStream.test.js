import { afterEach, describe, expect, it } from 'vitest';
import {
  ensureSessionState,
  agentActivityStatus,
  loadHistory,
} from '../chatState.js';
import { visibleTimelineItemsForRender } from '../chatTimeline.js';
import {
  DISPLAYED_AGENT_ID,
  DISPLAYED_SESSION_ID,
  activeRun,
  makeStreamHarness,
  serverRunEvent,
  timelineSnapshot,
  timelinePatch,
} from './chatRunStream.support.js';

const owners = [];
const setup = (options) => {
  const harness = makeStreamHarness(options);
  owners.push(harness.stream);
  return harness;
};
afterEach(() => {
  for (const owner of owners.splice(0)) owner.closeSubscriptions();
});
const identity = (runId, fields = {}) => ({
  run_id: runId,
  agent_id: DISPLAYED_AGENT_ID,
  session_id: DISPLAYED_SESSION_ID,
  ...fields,
});
const send = (harness, type, sequence, runId, fields = {}) =>
  harness.ws(serverRunEvent(type, sequence, identity(runId, fields)));

describe('server conversation projection transport', () => {
  it('opens one timeline connection and applies snapshots and suffix patches without changing another Run', () => {
    const h = setup();
    const session = h.displayedSession();
    const historical = {
      id: 'old',
      type: 'message',
      message: { role: 'user', content: 'Earlier' },
    };
    session.timelineItems = [historical];
    h.stream.applyConnectionSnapshot({ active_runs: [activeRun('run-one')] });
    h.stream.applyConnectionSnapshot({ active_runs: [activeRun('run-one')] });
    expect(h.subscriptions).toHaveLength(1);
    expect(h.subscriptions[0]).toMatchObject({
      sseUrl: '/api/runs/run-one/timeline',
      options: {
        eventTypes: ['timeline_snapshot', 'timeline_update'],
        closeOnTerminal: false,
      },
    });
    h.sse(timelineSnapshot('run-one'), timelinePatch('run-one', 10));
    expect(session.timelineItems[0]).toBe(historical);
    expect(visibleTimelineItemsForRender(session)[1].outputs[0].content).toBe(
      'Hello!',
    );
  });

  it('restores usage, controls and Iterations from a snapshot beyond retained raw events', () => {
    const h = setup();
    const speed = {
      output_tokens_per_second: 50,
      first_token_ms: 900,
      model: 'provider/first',
      message_id: 'first',
    };
    const latency = {
      first_token_ms: 20,
      model: 'provider/second',
      message_id: 'second',
    };
    h.stream.applyConnectionSnapshot({ active_runs: [activeRun('run-one')] });
    h.sse(
      timelineSnapshot('run-one', 5000, {
        state: {
          iteration_count: 7,
          controls: { compaction: 'pending' },
          controls_sequence: 5000,
          context_usage: { tokens: 15 },
          session_usage: { input_tokens: 22 },
          usage: { input_tokens: 3 },
          output_speed: speed,
          output_latency: latency,
        },
      }),
    );
    expect(h.displayedSession()).toMatchObject({
      currentRun: { iterationCount: 7, controls: { compaction: 'pending' } },
      contextUsage: { tokens: 15 },
      sessionUsage: { input_tokens: 22 },
      usage: { input_tokens: 3 },
      outputSpeed: speed,
      outputLatency: latency,
    });
    h.sse(
      timelinePatch('run-one', 5000, {
        status: 'cancelled',
        state: {
          usage: { speed: { first_token_ms: 20 } },
          output_speed: speed,
          output_latency: latency,
        },
      }),
    );
    expect(h.displayedSession().outputSpeed).toEqual(speed);
    expect(h.displayedSession().outputLatency).toEqual(latency);
  });

  it.each(['completed', 'failed', 'cancelled', 'interrupted'])(
    'keeps streaming through an early WebSocket %s until the final projection arrives',
    (status) => {
      const h = setup();
      h.stream.applyConnectionSnapshot({ active_runs: [activeRun('run-one')] });
      h.sse(timelineSnapshot('run-one'));
      const connection = h.subscriptions[0];
      send(h, `run_${status}`, 11, 'run-one', { status });
      expect(connection.close).not.toHaveBeenCalled();
      h.sse(timelinePatch('run-one', 10, { content: ' final', status }));
      expect(connection.close).toHaveBeenCalledOnce();
      expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
        'Hello final',
      );
      expect(h.displayedSession().currentRun.status).toBe(status);
      expect(h.reconcileRunSession).toHaveBeenCalled();
    },
  );

  it('ignores a replaced stream callback and old completed Run events after its successor starts', () => {
    const h = setup();
    h.stream.applyConnectionSnapshot({ active_runs: [activeRun('old')] });
    h.sse(timelineSnapshot('old'));
    const old = h.subscriptions[0];
    send(h, 'run_started', 1, 'new', { status: 'running' });
    h.displayedSession().historyRuns.old = { complete: true };
    send(h, 'run_started', 1, 'old', { status: 'running' });
    old.handlers.onEvent({
      type: 'timeline_snapshot',
      data: timelineSnapshot('old', 20, { content: 'late' }),
    });
    expect(old.close).toHaveBeenCalledOnce();
    expect(h.displayedSession().currentRun.runId).toBe('new');
    expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
      'Hello',
    );
    expect(h.subscriptions).toHaveLength(2);
  });

  it('retains its confirmed terminal status and output over a stale running History reply', () => {
    const h = setup();
    h.stream.applyConnectionSnapshot({ active_runs: [activeRun('run-one')] });
    h.sse(
      timelineSnapshot('run-one', 20, {
        status: 'completed',
        content: 'Final',
      }),
    );
    loadHistory(h.displayedSession(), [], {
      items: timelineSnapshot('run-one', 10).items,
      timelineSequence: 10,
      activeRunId: 'run-one',
      runs: [{ run_id: 'run-one', complete: false }],
    });
    expect(h.displayedSession().status).toBe('completed');
    expect(h.displayedSession().timelineItems[0].outputs[0].content).toBe(
      'Final',
    );
  });

  it('removes a consumed Queue item when the authoritative Run start names it', () => {
    const h = setup();
    h.displayedSession().queue = [{ id: 'item' }];
    send(h, 'run_started', 1, 'run-one', {
      status: 'running',
      output: { queue_item_id: 'item' },
    });
    expect(h.displayedSession().queue).toEqual([]);
  });

  it('keeps excluded Runs visible while excluding them from Agent activity', () => {
    const h = setup();
    h.stream.applyConnectionSnapshot({
      active_runs: [
        activeRun('quiet', { contributes_to_agent_activity: false }),
      ],
    });
    h.sse(timelineSnapshot('quiet'));
    expect(h.displayedSession().timelineItems).toHaveLength(1);
    expect(agentActivityStatus(h.chatState, DISPLAYED_AGENT_ID)).toBe('idle');
    h.sse(timelinePatch('quiet', 10, { status: 'completed' }));
    expect(h.displayedSession().hasUnreadCompletion).toBe(false);
    send(h, 'run_completed', 20, 'quiet', {
      status: 'completed',
      contributes_to_agent_activity: false,
    });
    expect(h.displayedSession().hasUnreadCompletion).toBe(false);
  });

  it('settles and releases non-displayed Runs without subscribing, and ignores late redelivery', () => {
    const h = setup({ isDisplayedSession: () => false });
    send(h, 'run_started', 1, 'background', { status: 'running' });
    send(h, 'run_completed', 40, 'background', { status: 'completed' });
    send(h, 'run_started', 1, 'background', { status: 'running' });
    expect(h.subscriptions).toHaveLength(0);
    expect(h.displayedSession()).toMatchObject({
      status: 'completed',
      hasUnreadCompletion: true,
      runEvents: [],
    });
    expect(h.reconcileRunSession).not.toHaveBeenCalled();
  });

  it('leaves no unread result for a completion the Session recorded read', () => {
    const h = setup({ isDisplayedSession: () => false });
    send(h, 'run_started', 1, 'to-user', { status: 'running' });
    send(h, 'run_completed', 2, 'to-user', { status: 'completed' });
    expect(h.displayedSession().hasUnreadCompletion).toBe(true);
    send(h, 'run_started', 1, 'to-parent', { status: 'running' });
    send(h, 'run_cancelled', 2, 'to-parent', {
      status: 'cancelled',
      completion_read: true,
    });
    expect(h.displayedSession()).toMatchObject({
      status: 'cancelled',
      hasUnreadCompletion: false,
      latestCompletionRunId: 'to-parent',
      unreadRunId: '',
    });
  });

  it('restores child activity from a connection snapshot and recovers a missing displayed Run', () => {
    const h = setup();
    h.stream.applyConnectionSnapshot({
      active_runs: [
        activeRun('run-one'),
        activeRun('child', { agent_id: 'worker', session_id: 'child-session' }),
      ],
    });
    expect(h.subscriptions).toHaveLength(1);
    expect(h.subAgentRunStatuses['run:child']).toBe('running');
    h.stream.applyConnectionSnapshot({ active_runs: [] });
    expect(h.reconcileRunSession).toHaveBeenCalledWith(
      h.displayedSession(),
      'run-one',
    );
    expect(
      ensureSessionState(h.chatState, 'worker', 'child-session').currentRun,
    ).toBeNull();
  });

  it('closes a deselected Session subscription and rejects its late callback', () => {
    const h = setup();
    h.stream.applyConnectionSnapshot({ active_runs: [activeRun('run-one')] });
    const old = h.subscriptions[0];
    h.stream.closeSubscriptionsExcept('other::session');
    old.handlers.onEvent({
      type: 'timeline_snapshot',
      data: timelineSnapshot('run-one'),
    });
    expect(h.displayedSession().timelineItems).toEqual([]);
    expect(old.close).toHaveBeenCalledOnce();
  });
});
