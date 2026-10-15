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
import {
  assistantRun,
  messageItem,
  outputItem,
  reasoningItem,
  toolItem,
} from './timelineItems.support.js';

// A bridge whose Alpha participant streams the active Run `run-stream`.
function streamingBridge() {
  const setup = createBridge(profile, swarmWithRun('run-stream'));
  setup.bridge.subscribeRun.mockResolvedValue({
    subscription_id: 'stream',
    timeline_version: 1,
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
const snapshot = (runId, sequence, content) => ({
  type: 'timeline_snapshot',
  run_id: runId,
  sequence,
  items: [assistantRun(runId, [outputItem(content, { streaming: true })])],
  state: { status: 'running' },
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

  it('keeps complete History visible until the authoritative snapshot and then applies new children', async () => {
    const { bridge } = streamingBridge();
    const user = message('user', 'user', 'request');
    const answer = message(
      'answer',
      'assistant',
      'Already loaded complete answer',
    );
    const items = [
      messageItem(user, { runId: 'run-stream' }),
      assistantRun('run-stream', [
        outputItem(answer.content, { id: 'answer' }),
      ]),
    ];
    bridge.readHistory.mockResolvedValue({ messages: [user, answer], items });
    await openActivity(bridge);
    expect(historyText()).toContain(answer.content);
    bridge.emitRun('stream', {
      type: 'timeline_snapshot',
      run_id: 'run-stream',
      sequence: 4,
      items,
      state: { status: 'running' },
    });
    await tick();
    expect(historyText().split(answer.content)).toHaveLength(2);
    bridge.emitRun('stream', {
      type: 'timeline_update',
      run_id: 'run-stream',
      sequence: 7,
      base_sequence: 4,
      run: { id: 'assistant-run-run-stream' },
      children: [
        toolItem({
          id: 'call',
          name: 'read',
          arguments: { path: 'demo.txt' },
          result: { ok: true, data: { content: 'done' } },
        }),
        outputItem('New live answer', { id: 'new-answer', streaming: true }),
      ],
    });
    await vi.waitFor(() => expect(historyText()).toContain('New live answer'));
    expect(historyText()).toContain(answer.content);
  });

  it('retains final output across a stale History reply and ignores a stale active snapshot', async () => {
    const { bridge } = streamingBridge();
    const user = message('user', 'user', 'request');
    const first = message('first', 'assistant', 'First step');
    const final = message('final', 'assistant', 'Final streamed answer');
    bridge.readHistory.mockResolvedValue({
      messages: [user, first],
      items: [
        messageItem(user, { runId: 'run-stream' }),
        assistantRun('run-stream', [
          outputItem(first.content, { id: 'first' }),
        ]),
      ],
      active_run: { run_id: 'run-stream' },
      timeline_sequence: 2,
    });
    await openActivity(bridge);
    bridge.emitRun('stream', {
      type: 'timeline_snapshot',
      run_id: 'run-stream',
      sequence: 5,
      items: [
        messageItem(user, { runId: 'run-stream' }),
        assistantRun(
          'run-stream',
          [
            outputItem(first.content, { id: 'first' }),
            outputItem(final.content, { id: 'final' }),
          ],
          { status: 'completed' },
        ),
      ],
      state: { status: 'completed' },
      terminal: true,
    });
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(2));
    expect(historyText()).toContain(final.content);
    bridge.invalidate();
    await settle(160);
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);
    expect(historyText()).toContain(final.content);
  });

  it('keeps expanded thinking open while semantic text patches grow and rejects duplicate chunks', async () => {
    const { bridge } = streamingBridge();
    bridge.readHistory.mockResolvedValue({ messages: [], items: [] });
    await openActivity(bridge);
    bridge.emitRun('stream', {
      type: 'timeline_snapshot',
      run_id: 'run-stream',
      sequence: 1,
      items: [
        assistantRun('run-stream', [
          reasoningItem('Thinking prefix', { streaming: true }),
        ]),
      ],
      state: { status: 'running' },
    });
    await vi.waitFor(() =>
      expect(document.querySelector('.reasoning-block')).not.toBeNull(),
    );
    const disclosure = document.querySelector('.reasoning-block');
    disclosure.open = true;
    disclosure.dispatchEvent(new Event('toggle'));
    await tick();
    for (let i = 2; i <= 150; i++) {
      const chunk = {
        type: 'timeline_update',
        run_id: 'run-stream',
        sequence: i,
        base_sequence: i - 1,
        run: { id: 'assistant-run-run-stream' },
        child_updates: [
          { id: 'reasoning-one', append: { content: ` chunk-${i}` } },
        ],
      };
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
    bridge.readHistory.mockResolvedValue({ messages: [], items: [] });
    bridge.subscribeRun
      .mockResolvedValueOnce({
        timeline_version: 1,
        subscription_id: 'first-stream',
      })
      .mockResolvedValue({
        timeline_version: 1,
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
    const live = snapshot('refresh-run', 1, 'live-refresh-sentinel');
    bridge.emitRun(
      'second-stream',
      snapshot('refresh-run', 1, 'stale-stream-sentinel'),
    );
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
        timeline_version: 1,
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
      snapshot('run-retry', 1, 'Recovered output'),
    );
    await vi.waitFor(() => expect(historyText()).toContain('Recovered output'));
  });

  it('keeps rendered live content and its subscription across peer changes, then attaches a new Run', async () => {
    const { bridge, operation } = createBridge();
    let runId = 'run-first';
    overrideOperations(operation, {
      'swarms.get': () => ({ swarm: swarmWithRun(runId) }),
    });
    bridge.readHistory.mockResolvedValue({ messages: [], items: [] });
    bridge.subscribeRun.mockImplementation(async (_group, run) => ({
      timeline_version: 1,
      subscription_id: run,
    }));
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
    bridge.emitRun(runId, snapshot(runId, 1, 'Stable live output'));
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
        0,
        { timeline: true },
      ),
    );
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith('run-first');
    bridge.emitRun('run-first', snapshot('run-first', 2, 'Stale output'));
    bridge.emitRun(runId, snapshot(runId, 1, 'New live output'));
    await tick();
    await vi.waitFor(() => expect(historyText()).toContain('New live output'));
    expect(historyText()).not.toContain('Stale output');
  });

  it('follows the Run only while Activity shows and catches up once when it shows again', async () => {
    const { bridge, operation } = createBridge();
    let detail = swarmWithRun('run-first');
    overrideOperations(operation, { 'swarms.get': () => ({ swarm: detail }) });
    bridge.readHistory.mockResolvedValue({ messages: [], items: [] });
    bridge.subscribeRun.mockImplementation(async (_group, run) => ({
      timeline_version: 1,
      subscription_id: run,
    }));
    const showTab = (label) =>
      [...document.querySelectorAll('[role="tab"]')]
        .find((tab) => tab.textContent.includes(label))
        .click();
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
    bridge.emitRun('run-first', snapshot('run-first', 1, 'Live before hiding'));
    await vi.waitFor(() =>
      expect(historyText()).toContain('Live before hiding'),
    );

    showTab(t('swarm.tabs.board'));
    await vi.waitFor(() =>
      expect(bridge.unsubscribeRun).toHaveBeenCalledWith('run-first'),
    );
    // The Run settles while Activity is hidden.
    detail = swarmWithRun('run-first', { run_active: false });
    bridge.readHistory.mockResolvedValue({
      status: 'completed',
      items: [
        assistantRun(
          'run-first',
          [outputItem('Live before hiding, then done.')],
          { source: 'history', status: 'completed' },
        ),
      ],
      messages: [
        message('final', 'assistant', 'Live before hiding, then done.'),
      ],
    });
    await refreshSwarm(bridge, operation, 3);
    await settle();
    expect(bridge.readHistory).toHaveBeenCalledTimes(1);
    showTab(t('swarm.tabs.activity'));
    await vi.waitFor(() => expect(historyText()).toContain('then done.'));
    expect(historyText().split('Live before hiding')).toHaveLength(2);
    await settle();
    expect(bridge.readHistory).toHaveBeenCalledTimes(2);
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);

    // A Run that started while Activity was hidden is followed once it shows.
    showTab(t('swarm.tabs.board'));
    detail = swarmWithRun('run-second');
    await refreshSwarm(bridge, operation);
    await settle();
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);
    showTab(t('swarm.tabs.activity'));
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenLastCalledWith(
        'swr-a',
        'run-second',
        0,
        { timeline: true },
      ),
    );
    expect(bridge.readHistory).toHaveBeenCalledTimes(3);
  });
});
