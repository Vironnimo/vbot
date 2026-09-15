// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  button,
  createBridge,
  render,
  swarm,
  tick,
} from './SwarmPage.support.js';

function streamingBridge() {
  const setup = createBridge();
  const original = setup.operation.getMockImplementation();
  setup.operation.mockImplementation((name, args) =>
    name === 'swarms.get'
      ? Promise.resolve({
          swarm: {
            ...swarm,
            participants: [
              {
                ...swarm.participants[0],
                lifecycle_run_id: 'run-stream',
                run_active: true,
              },
            ],
          },
        })
      : original(name, args),
  );
  setup.bridge.subscribeRun.mockResolvedValue({
    subscription_id: 'stream',
    replay_through_sequence: 0,
  });
  return setup;
}
async function openActivity(bridge) {
  await render(bridge);
  button('Investigate').click();
  await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
  button('Alpha').click();
  await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
}
const message = (id, role, content) => ({
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
const transcript = () => document.querySelector('.history').textContent;

describe('Swarm streaming continuity', () => {
  it('does not restart inspection while the Model catalog is still loading', async () => {
    const { bridge, operation } = streamingBridge();
    const original = operation.getMockImplementation();
    let resolveCatalog;
    operation.mockImplementation((name, args) =>
      name === 'catalog'
        ? new Promise((resolve) => {
            resolveCatalog = () => resolve(original(name, args));
          })
        : original(name, args),
    );
    await render(bridge);
    button('Investigate').click();
    await vi.waitFor(() => expect(button('Alpha')).toBeDefined());
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.history')).not.toBeNull(),
    );
    const before = operation.mock.calls.filter(
      ([name]) => name === 'swarms.get',
    ).length;
    bridge.invalidate();
    await vi.waitFor(() =>
      expect(
        operation.mock.calls.filter(([name]) => name === 'swarms.get').length,
      ).toBeGreaterThan(before),
    );
    expect(bridge.readHistory).toHaveBeenCalledTimes(1);
    expect(
      operation.mock.calls.filter(([name]) => name === 'catalog'),
    ).toHaveLength(1);
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
    expect(transcript()).toContain(answer.content);
    for (const replay of [
      event(1, 'run_started'),
      event(2, 'user_message_persisted', { message: user }),
      event(3, 'assistant_output_delta', { content_delta: 'Already' }),
    ]) {
      bridge.emitRun('stream', replay);
      await new Promise((resolve) => setTimeout(resolve, 45));
      await tick();
      expect(transcript()).toContain(answer.content);
      expect(transcript().split(answer.content)).toHaveLength(2);
    }
    bridge.emitRun('stream', event(4, 'assistant_output', { message: answer }));
    await tick();
    expect(transcript().split(answer.content)).toHaveLength(2);
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
    await vi.waitFor(() => expect(transcript()).toContain('New live answer'));
    expect(transcript()).toContain(answer.content);
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
    expect(transcript()).toContain(final.content);
    bridge.invalidate();
    await new Promise((resolve) => setTimeout(resolve, 160));
    expect(bridge.subscribeRun).toHaveBeenCalledTimes(1);
    expect(transcript()).toContain(final.content);
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
    button('New run').click();
    await tick();
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith('stream');
    expect(document.querySelector('.history')).toBeNull();
  });
});
