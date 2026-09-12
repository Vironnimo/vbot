// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  swarm,
  button,
  createBridge,
  render,
} from './SwarmPage.support.js';

describe('Swarm selection reconciliation', () => {
  it('does not overwrite a newly selected discussion with a late previous read', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let finishMain;
    operation.mockImplementation((name, args) => {
      if (name === 'board.read') {
        if (args.discussion_id === 'dsc-main')
          return new Promise((resolve) => {
            finishMain = resolve;
          });
        return Promise.resolve({
          entries: [
            {
              id: 'review-new',
              sender_id: 'prt-a',
              text: 'new-discussion-sentinel',
            },
          ],
        });
      }
      return original(name, args);
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(finishMain).toBeTypeOf('function'));
    const dropdown = document.getElementById('swarm-discussion');
    dropdown.value = 'dsc-findings';
    dropdown.dispatchEvent(new Event('change', { bubbles: true }));
    await vi.waitFor(() =>
      expect(document.querySelector('.board').textContent).toContain(
        'new-discussion-sentinel',
      ),
    );
    finishMain({
      entries: [
        {
          id: 'review-old',
          sender_id: 'prt-a',
          text: 'old-discussion-sentinel',
        },
      ],
    });
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    expect(document.querySelector('.board').textContent).toContain(
      'new-discussion-sentinel',
    );
  });

  it('refreshes pending counts while the participant stays in the same active Run', async () => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    let pendingCount = 12;
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.get')
        return Promise.resolve({
          swarm: {
            ...structuredClone(swarm),
            participants: swarm.participants.map((participant) => ({
              ...participant,
              lifecycle_run_id: 'run-pending',
              pending_count: participant.id === 'prt-a' ? pendingCount : 0,
            })),
          },
        });
      return original(name, args);
    });
    await render(bridge);
    button('Investigate').click();
    const pending = () => button('Alpha')?.querySelector('small')?.textContent;
    await vi.waitFor(() => expect(pending()).toMatch(/12\s+pending/));
    for (const remaining of [8, 4, 0]) {
      pendingCount = remaining;
      bridge.invalidate();
      await vi.waitFor(() =>
        expect(pending()).toMatch(
          new RegExp(`running.*${remaining}\\s+pending`),
        ),
      );
    }
    button('Alpha').click();
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalled());
    pendingCount = 2;
    bridge.invalidate();
    await vi.waitFor(() => expect(pending()).toMatch(/2\s+pending/));
  });

  it('reloads selected Session history when invalidated with an unchanged Run id', async () => {
    const { bridge, operation } = createBridge();
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
    button('Alpha').click();
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(1));
    const before = operation.mock.calls.filter(
      ([name]) => name === 'swarms.usage',
    ).length;
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(
        operation.mock.calls.filter(([name]) => name === 'swarms.usage').length,
      ).toBeGreaterThan(before),
    );
    await tick();
    expect(bridge.readHistory).toHaveBeenCalledTimes(2);
  });
});

it('loads older Session pages in canonical order and retains their depth on refresh', async () => {
  const { bridge } = createBridge();
  const entry = (n) => ({
    id: `history-${n}`,
    role: 'user',
    content: `history-sentinel-${n}`,
    timestamp: '2026-09-09T10:00:00+00:00',
  });
  bridge.readHistory.mockImplementation(async (_swarm, _participant, query) => {
    const first =
      query.before === 'oldest' ? 0 : query.before === 'middle' ? 2 : 4;
    return {
      messages: [entry(first), entry(first + 1)],
      has_more: first > 0,
      next_before: first === 4 ? 'middle' : first === 2 ? 'oldest' : null,
    };
  });
  await render(bridge);
  button('Investigate').click();
  await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
  button('Alpha').click();
  await vi.waitFor(() => expect(button('Load older messages')).toBeDefined());
  button('Load older messages').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.history').textContent).toContain(
      'history-sentinel-2',
    ),
  );
  button('Load older messages').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.history').textContent).toContain(
      'history-sentinel-0',
    ),
  );
  expect(button('Load older messages')).toBeUndefined();
  expect(bridge.readHistory).toHaveBeenNthCalledWith(2, 'swr-a', 'prt-a', {
    limit: 100,
    before: 'middle',
  });
  expect(bridge.readHistory).toHaveBeenNthCalledWith(3, 'swr-a', 'prt-a', {
    limit: 100,
    before: 'oldest',
  });
  bridge.invalidate();
  await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(6));
  await tick();
  const text = document.querySelector('.history').textContent;
  const positions = Array.from({ length: 6 }, (_, n) =>
    text.indexOf(`history-sentinel-${n}`),
  );
  expect(positions.every((value) => value >= 0)).toBe(true);
  expect(positions).toEqual([...positions].sort((a, b) => a - b));
});

it('ignores an older Session page that arrives after selecting another participant', async () => {
  const { bridge } = createBridge();
  let finishOlder;
  bridge.readHistory.mockImplementation(async (_swarm, participant, query) => {
    if (query.before)
      return new Promise((resolve) => {
        finishOlder = resolve;
      });
    return {
      messages: [{ id: participant, role: 'user', content: participant }],
      has_more: participant === 'prt-a',
      next_before: 'older',
    };
  });
  await render(bridge);
  button('Investigate').click();
  await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
  button('Alpha').click();
  await vi.waitFor(() => expect(button('Load older messages')).toBeDefined());
  button('Load older messages').click();
  await vi.waitFor(() => expect(finishOlder).toBeTypeOf('function'));
  button('Beta').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.history').textContent).toContain('prt-b'),
  );
  finishOlder({
    messages: [
      { id: 'older-alpha', role: 'user', content: 'stale-alpha-sentinel' },
    ],
    has_more: false,
  });
  await new Promise((resolve) => setTimeout(resolve));
  await tick();
  expect(document.querySelector('.history').textContent).toContain('prt-b');
  expect(document.querySelector('.history').textContent).not.toContain(
    'stale-alpha-sentinel',
  );
});

it('ignores old Swarm detail replies after selecting a new Swarm', async () => {
  const { bridge, operation } = createBridge();
  const original = operation.getMockImplementation();
  const other = {
    ...structuredClone(swarm),
    id: 'swr-b',
    prompt: 'Second swarm',
    main_discussion_id: 'dsc-b',
  };
  let finishDiscussions;
  operation.mockImplementation((name, args) => {
    if (name === 'swarms.list')
      return Promise.resolve({ entries: [swarm, other] });
    if (name === 'swarms.get')
      return Promise.resolve({
        swarm: structuredClone(args.swarm_id === 'swr-b' ? other : swarm),
      });
    if (name === 'board.list') {
      if (args.swarm_id === 'swr-a')
        return new Promise((resolve) => {
          finishDiscussions = resolve;
        });
      return Promise.resolve({
        entries: [{ id: 'dsc-b', title: 'Second discussion' }],
      });
    }
    if (name === 'board.read')
      return Promise.resolve({
        entries: [
          { id: args.swarm_id, text: args.swarm_id, sender_id: 'prt-a' },
        ],
      });
    return original(name, args);
  });
  await render(bridge);
  button('Investigate').click();
  await vi.waitFor(() => expect(finishDiscussions).toBeTypeOf('function'));
  button('Second swarm').click();
  await vi.waitFor(() =>
    expect(document.querySelector('.board').textContent).toContain('swr-b'),
  );
  finishDiscussions({ entries: [{ id: 'dsc-main', title: 'Old discussion' }] });
  await new Promise((resolve) => setTimeout(resolve));
  await tick();
  expect(document.querySelector('.board').textContent).toContain('swr-b');
  expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/swarms/swr-b');
  expect(document.getElementById('swarm-discussion').value).toBe('dsc-b');
});

it.each(['swarms.events', 'swarms.usage'])(
  'ignores late %s from the previously selected Swarm',
  async (method) => {
    const { bridge, operation } = createBridge();
    const original = operation.getMockImplementation();
    const other = {
      ...structuredClone(swarm),
      id: 'swr-b',
      prompt: 'Second swarm',
    };
    const pending = [];
    const result = (id) =>
      method === 'swarms.events'
        ? { entries: [{ id, kind: `${id}-event-sentinel`, actor: 'user' }] }
        : {
            usage: {
              usage: {
                totals: {
                  measured_input_tokens: id === 'swr-a' ? 111 : 222,
                  measured_output_tokens: 0,
                  estimated_input_tokens: 0,
                  estimated_output_tokens: 0,
                },
                models: [],
              },
              tools: { total_calls: 0 },
            },
          };
    operation.mockImplementation((name, args) => {
      if (name === 'swarms.list')
        return Promise.resolve({ entries: [swarm, other] });
      if (name === 'swarms.get')
        return Promise.resolve({
          swarm: structuredClone(args.swarm_id === 'swr-b' ? other : swarm),
        });
      if (name === method) {
        if (args.swarm_id === 'swr-a')
          return new Promise((resolve) => pending.push(resolve));
        return Promise.resolve(result(args.swarm_id));
      }
      return original(name, args);
    });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0));
    button('Second swarm').click();
    await vi.waitFor(() =>
      expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/swarms/swr-b'),
    );
    button(method === 'swarms.events' ? 'Delivery audit' : 'Usage').click();
    await tick();
    const selector = method === 'swarms.events' ? '.audit' : '.usage-summary';
    const current = method === 'swarms.events' ? 'swr-b-event-sentinel' : '222';
    expect(document.querySelector(selector).textContent).toContain(current);
    for (const resolve of pending) resolve(result('swr-a'));
    await new Promise((resolve) => setTimeout(resolve));
    await tick();
    expect(document.querySelector(selector).textContent).toContain(current);
    expect(bridge.replaceRoute).toHaveBeenLastCalledWith('/swarms/swr-b');
  },
);

it.each([false, true])(
  'reattaches the same Run on refresh and ignores duplicate stream events (cleanup fails: %s)',
  async (cleanupFails) => {
    const { bridge, operation } = createBridge();
    if (cleanupFails)
      bridge.unsubscribeRun.mockRejectedValue(
        new Error('detached-stream-sentinel'),
      );
    const original = operation.getMockImplementation();
    operation.mockImplementation((name, args) =>
      name === 'swarms.get'
        ? Promise.resolve({
            swarm: {
              ...structuredClone(swarm),
              participants: [
                {
                  ...swarm.participants[0],
                  lifecycle_run_id: 'refresh-run',
                  run_active: true,
                },
              ],
            },
          })
        : original(name, args),
    );
    bridge.readHistory.mockResolvedValue({ messages: [] });
    bridge.subscribeRun
      .mockResolvedValueOnce({ subscription_id: 'first-stream' })
      .mockResolvedValue({ subscription_id: 'second-stream' });
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(1),
    );
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(bridge.subscribeRun).toHaveBeenCalledTimes(2),
    );
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith('first-stream');
    const event = {
      type: 'assistant_output_delta',
      run_id: 'refresh-run',
      sequence: 1,
      payload: { content_delta: 'live-refresh-sentinel' },
    };
    bridge.emitRun('first-stream', {
      ...event,
      payload: { content_delta: 'stale-stream-sentinel' },
    });
    bridge.emitRun('second-stream', event);
    bridge.emitRun('second-stream', event);
    await tick();
    const text = document.querySelector('.history').textContent;
    expect(text).not.toContain('stale-stream-sentinel');
    expect(text.split('live-refresh-sentinel')).toHaveLength(2);
  },
);
