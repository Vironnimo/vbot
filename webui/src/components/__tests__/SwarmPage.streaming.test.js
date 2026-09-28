// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  profile,
  button,
  createBridge,
  overrideOperations,
  swarmWithRun,
  callsTo,
  settle,
  openParticipant,
  historyText,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';

// A bridge whose Alpha participant streams the active Run `run-stream`.
function streamingBridge() {
  const setup = createBridge(profile, swarmWithRun('run-stream'));
  setup.bridge.subscribeRun.mockResolvedValue({
    subscription_id: 'stream',
    replay_through_sequence: 0,
  });
  return setup;
}
async function openActivity(bridge) {
  await openParticipant(bridge);
  await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
}
const message = (id, role, content) => ({
  history_run_id: 'run-stream',
  id,
  role,
  content,
  timestamp: '2026-09-15T10:00:00Z',
});
const event = (sequence, type, payload = {}) => ({
  sequence,
  type,
  payload,
  run_id: 'run-stream',
  timestamp: '2026-09-15T10:00:00Z',
});
const delta = (runId, sequence, content) => ({
  type: 'assistant_output_delta',
  run_id: runId,
  sequence,
  payload: { content_delta: content },
});
// Invalidates the page and waits until it has read the Swarm again.
async function refreshSwarm(bridge, operation, times = 1) {
  const before = callsTo(operation, 'swarms.get').length;
  for (let i = 0; i < times; i++) bridge.invalidate();
  await vi.waitFor(() =>
    expect(callsTo(operation, 'swarms.get').length).toBeGreaterThan(before),
  );
}

describe('Swarm streaming continuity', () => {
  it('does not restart inspection while the Model catalog is still loading', async () => {
    const { bridge, operation } = streamingBridge();
    let resolveCatalog;
    overrideOperations(operation, {
      catalog: (_args, fallback) =>
        new Promise((resolve) => {
          resolveCatalog = () => resolve(fallback());
        }),
    });
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(document.querySelector('.history')).not.toBeNull(),
    );
    await refreshSwarm(bridge, operation);
    expect(bridge.readHistory).toHaveBeenCalledTimes(1);
    expect(callsTo(operation, 'catalog')).toHaveLength(1);
    resolveCatalog();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
  });

  it('keeps the complete History visible until replay reaches its server watermark', async () => {
    const { bridge } = streamingBridge();
    const user = message('user', 'user', 'request');
    const answer = message(
      'answer',
      'assistant',
      'Already loaded complete answer',
    );
    bridge.readHistory.mockResolvedValue({ messages: [user, answer] });
    bridge.subscribeRun.mockResolvedValue({
      subscription_id: 'stream',
      replay_through_sequence: 4,
    });
    await openActivity(bridge);
    expect(historyText()).toContain(answer.content);
    for (const replay of [
      event(1, 'run_started'),
      event(2, 'user_message_persisted', { message: user }),
      event(3, 'assistant_output_delta', { content_delta: 'Already' }),
    ]) {
      bridge.emitRun('stream', replay);
      await settle(45);
      expect(historyText().split(answer.content)).toHaveLength(2);
    }
    bridge.emitRun('stream', event(4, 'assistant_output', { message: answer }));
    await tick();
    expect(historyText().split(answer.content)).toHaveLength(2);
    bridge.emitRun(
      'stream',
      event(5, 'tool_call_started', {
        tool_call: {
          id: 'call',
          name: 'read',
          arguments: { path: 'demo.txt' },
        },
      }),
    );
    bridge.emitRun(
      'stream',
      event(6, 'tool_call_result', {
        tool_call_id: 'call',
        message: message('result', 'tool', 'done'),
      }),
    );
    bridge.emitRun(
      'stream',
      event(7, 'assistant_output_delta', { content_delta: 'New live answer' }),
    );
    await vi.waitFor(() => expect(historyText()).toContain('New live answer'));
    expect(historyText()).toContain(answer.content);
  });

  it('retains final output across a stale History reply and ignores a stale active snapshot', async () => {
    const { bridge } = streamingBridge();
    const user = message('user', 'user', 'request');
    const first = message('first', 'assistant', 'First step');
    const final = message('final', 'assistant', 'Final streamed answer');
    bridge.readHistory.mockResolvedValue({ messages: [user, first] });
    await openActivity(bridge);
    for (const entry of [
      event(1, 'user_message_persisted', { message: user }),
      event(2, 'assistant_output', { message: first }),
      event(3, 'assistant_output_delta', { content_delta: final.content }),
      event(4, 'assistant_output', { message: final }),
      event(5, 'run_completed'),
    ])
      bridge.emitRun('stream', entry);
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(2));
    expect(historyText()).toContain(final.content);
    bridge.invalidate();
    await settle(160);
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);
    expect(historyText()).toContain(final.content);
  });

  it('keeps expanded thinking open while compressed output grows and rejects duplicate chunks', async () => {
    const { bridge } = streamingBridge();
    bridge.readHistory.mockResolvedValue({ messages: [] });
    await openActivity(bridge);
    bridge.emitRun(
      'stream',
      event(1, 'reasoning_delta', { reasoning_delta: 'Thinking prefix' }),
    );
    await vi.waitFor(() =>
      expect(document.querySelector('.reasoning-block')).not.toBeNull(),
    );
    const disclosure = document.querySelector('.reasoning-block');
    disclosure.open = true;
    disclosure.dispatchEvent(new Event('toggle'));
    await tick();
    for (let i = 2; i <= 150; i++) {
      const chunk = event(i, 'reasoning_delta', {
        reasoning_delta: ` chunk-${i}`,
      });
      bridge.emitRun('stream', chunk);
      bridge.emitRun('stream', chunk);
    }
    await vi.waitFor(() =>
      expect(disclosure.textContent).toContain('chunk-150'),
    );
    expect(document.querySelector('.reasoning-block')).toBe(disclosure);
    expect(disclosure.open).toBe(true);
    expect(disclosure.textContent.split('chunk-150')).toHaveLength(2);
    button(t('swarm.newRun')).click();
    await tick();
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith('stream');
    expect(document.querySelector('.history')).toBeNull();
  });
});

describe('Swarm Run subscriptions across refreshes', () => {
  it('keeps selected Session history when invalidated with an unchanged Run id', async () => {
    const { bridge, operation } = createBridge();
    await openParticipant(bridge);
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(1));
    await refreshSwarm(bridge, operation);
    await tick();
    expect(bridge.readHistory).toHaveBeenCalledTimes(1);
  });

  it('retains the same Run subscription on refresh and ignores stale and duplicate events', async () => {
    const { bridge, operation } = createBridge(
      profile,
      swarmWithRun('refresh-run'),
    );
    bridge.readHistory.mockResolvedValue({ messages: [] });
    bridge.subscribeRun
      .mockResolvedValueOnce({
        replay_through_sequence: 0,
        subscription_id: 'first-stream',
      })
      .mockResolvedValue({
        replay_through_sequence: 0,
        subscription_id: 'second-stream',
      });
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
    await refreshSwarm(bridge, operation);
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);
    expect(bridge.readHistory).toHaveBeenCalledTimes(1);
    expect(bridge.unsubscribeRun).not.toHaveBeenCalled();
    const live = delta('refresh-run', 1, 'live-refresh-sentinel');
    bridge.emitRun('second-stream', {
      ...live,
      payload: { content_delta: 'stale-stream-sentinel' },
    });
    bridge.emitRun('first-stream', live);
    bridge.emitRun('first-stream', live);
    await tick();
    await vi.waitFor(() =>
      expect(historyText()).toContain('live-refresh-sentinel'),
    );
    expect(historyText()).not.toContain('stale-stream-sentinel');
    expect(historyText().split('live-refresh-sentinel')).toHaveLength(2);
  });

  it('retries a failed subscription on a later invalidation', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-retry'));
    bridge.subscribeRun
      .mockRejectedValueOnce(new Error('test-owned stream failure'))
      .mockResolvedValue({
        replay_through_sequence: 0,
        subscription_id: 'stream-recovered',
      });
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('test-owned stream failure'),
    );
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(2),
    );
    bridge.emitRun(
      'stream-recovered',
      delta('run-retry', 1, 'Recovered output'),
    );
    await vi.waitFor(() => expect(historyText()).toContain('Recovered output'));
  });

  it('keeps rendered live content and its subscription across peer changes, then attaches a new Run', async () => {
    const { bridge, operation } = createBridge();
    let runId = 'run-first';
    overrideOperations(operation, {
      'swarms.get': () => ({ swarm: swarmWithRun(runId) }),
    });
    bridge.readHistory.mockResolvedValue({ messages: [] });
    bridge.subscribeRun.mockImplementation(async (_group, run) => ({
      replay_through_sequence: 0,
      subscription_id: run,
    }));
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
    bridge.emitRun(runId, delta(runId, 1, 'Stable live output'));
    await tick();
    await vi.waitFor(() =>
      expect(document.querySelector('.history .streaming-text')).not.toBeNull(),
    );
    const content = document.querySelector('.history .streaming-text');
    expect(content.textContent).toContain('Stable live output');
    await refreshSwarm(bridge, operation, 90);
    await tick();
    expect(document.querySelector('.history .streaming-text')).toBe(content);
    expect(content.textContent).toContain('Stable live output');
    expect(bridge.readHistory).toHaveBeenCalledTimes(1);
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);
    expect(bridge.unsubscribeRun).not.toHaveBeenCalled();
    runId = 'run-second';
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenLastCalledWith(
        'swr-a',
        'run-second',
      ),
    );
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith('run-first');
    bridge.emitRun('run-first', delta('run-first', 2, 'Stale output'));
    bridge.emitRun(runId, delta(runId, 1, 'New live output'));
    await tick();
    await vi.waitFor(() => expect(historyText()).toContain('New live output'));
    expect(historyText()).not.toContain('Stale output');
  });
});
