// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  button,
  createBridge,
  render,
  swarm,
  tick,
  unmount,
  fixtureState,
} from './SwarmPage.support.js';
import { createPageRefresh } from '../../../../resources/extensions/swarm/ui/pageRefresh.js';

describe('Swarm refresh under load', () => {
  it('retries a failed subscription on a later invalidation', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    operation.mockImplementation((name, args) =>
      name === 'swarms.get'
        ? Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              participants: [
                {
                  ...swarm.participants[0],
                  lifecycle_run_id: 'run-retry',
                  run_active: true,
                },
              ],
            },
          })
        : original(name, args),
    );
    bridge.subscribeRun
      .mockRejectedValueOnce(new Error('test-owned stream failure'))
      .mockResolvedValue({
        replay_through_sequence: 0,
        subscription_id: 'stream-recovered',
      });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain('test-owned stream failure'),
    );
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(2),
    );
    bridge.emitRun('stream-recovered', {
      type: 'assistant_output_delta',
      run_id: 'run-retry',
      sequence: 1,
      payload: { content_delta: 'Recovered output' },
    });
    await vi.waitFor(() =>
      expect(document.querySelector('.history').textContent).toContain(
        'Recovered output',
      ),
    );
  });

  it('bounds overlapping invalidations and still refreshes during continuous activity', async () => {
    vi.useFakeTimers();
    const finishes = [];
    const refresh = vi.fn(
      () => new Promise((resolve) => finishes.push(resolve)),
    );
    const updates = createPageRefresh(refresh);
    for (let i = 0; i < 90; i++) updates.schedule();
    await vi.advanceTimersByTimeAsync(100);
    expect(refresh).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 90; i++) {
      updates.schedule();
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(refresh).toHaveBeenCalledTimes(1);
    finishes[0]();
    await vi.advanceTimersByTimeAsync(100);
    expect(refresh).toHaveBeenCalledTimes(2);
    updates.schedule();
    updates.destroy();
    finishes[1]();
    await vi.advanceTimersByTimeAsync(1000);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it.each(['Wiki', 'Decisions'])(
    'opens %s with nine Agents without waiting for hidden reports',
    async (tab) => {
      const { bridge, operation } = createBridge();
      const original = operation.getMockImplementation();
      const method = tab.toLowerCase();
      let finishList;
      operation.mockImplementation((name, args) => {
        if (name === 'swarms.get')
          return Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              participants: Array.from({ length: 9 }, (_, index) => ({
                ...swarm.participants[0],
                id: `peer-${index}`,
                display_name: `Peer ${index}`,
                run_active: true,
                lifecycle_run_id: `run-${index}`,
              })),
            },
          });
        if (name === 'swarms.usage' || name === 'swarms.events')
          return new Promise(() => {});
        if (name === method && args.action === 'list')
          return new Promise((resolve) => {
            finishList = resolve;
          });
        return original(name, args);
      });
      await render(bridge);
      button('Investigate').click();
      await vi.waitFor(() => expect(button(tab)).toBeDefined());
      button(tab).click();
      await vi.waitFor(() => expect(finishList).toBeTypeOf('function'));
      const listCalls = () =>
        operation.mock.calls.filter(([name]) => name === method).length;
      for (let i = 0; i < 90; i++) bridge.invalidate();
      await new Promise((resolve) => setTimeout(resolve, 150));
      expect(listCalls()).toBe(1);
      const entry =
        method === 'wiki'
          ? { page_id: 'wpg-ready', title: 'Loaded under load', revision: 1 }
          : {
              question_id: 'dec-ready',
              title: 'Loaded under load',
              revision: 1,
            };
      finishList({ entries: [entry] });
      await vi.waitFor(() =>
        expect(document.body.textContent).toContain('Loaded under load'),
      );
      await vi.waitFor(() => expect(listCalls()).toBe(2));
      expect(
        operation.mock.calls.filter(([name]) =>
          ['swarms.usage', 'swarms.events'].includes(name),
        ),
      ).toHaveLength(0);
      const calls = operation.mock.calls.length;
      fixtureState.mounted = await unmount(fixtureState.mounted);
      finishList({ entries: [] });
      await new Promise((resolve) => setTimeout(resolve, 150));
      expect(operation.mock.calls).toHaveLength(calls);
    },
  );

  it('keeps rendered live content and its subscription across peer changes, then attaches a new Run', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let runId = 'run-first';
    operation.mockImplementation((name, args) =>
      name === 'swarms.get'
        ? Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              participants: [
                {
                  ...swarm.participants[0],
                  lifecycle_run_id: runId,
                  run_active: true,
                },
              ],
            },
          })
        : original(name, args),
    );
    bridge.readHistory.mockResolvedValue({ messages: [] });
    bridge.subscribeRun.mockImplementation(async (_group, run) => ({
      replay_through_sequence: 0,
      subscription_id: run,
    }));
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
    bridge.emitRun(runId, {
      type: 'assistant_output_delta',
      run_id: runId,
      sequence: 1,
      payload: { content_delta: 'Stable live output' },
    });
    await tick();
    await vi.waitFor(() =>
      expect(document.querySelector('.history .streaming-text')).not.toBeNull(),
    );
    const content = document.querySelector('.history .streaming-text');
    expect(content.textContent).toContain('Stable live output');
    const before = operation.mock.calls.filter(
      ([name]) => name === 'swarms.get',
    ).length;
    for (let i = 0; i < 90; i++) bridge.invalidate();
    await vi.waitFor(() =>
      expect(
        operation.mock.calls.filter(([name]) => name === 'swarms.get').length,
      ).toBeGreaterThan(before),
    );
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
    bridge.emitRun('run-first', {
      type: 'assistant_output_delta',
      run_id: 'run-first',
      sequence: 2,
      payload: { content_delta: 'Stale output' },
    });
    bridge.emitRun(runId, {
      type: 'assistant_output_delta',
      run_id: runId,
      sequence: 1,
      payload: { content_delta: 'New live output' },
    });
    await tick();
    await vi.waitFor(() =>
      expect(document.querySelector('.history').textContent).toContain(
        'New live output',
      ),
    );
    expect(document.querySelector('.history').textContent).not.toContain(
      'Stale output',
    );
  });
});
