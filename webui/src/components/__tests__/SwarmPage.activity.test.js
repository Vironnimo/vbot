// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  profile,
  swarm,
  button,
  createBridge,
  overrideOperations,
  swarmWithRun,
  settle,
  render,
  openSwarm,
  openParticipant,
  historyText,
} from './SwarmPage.support.js';
import { t } from '../../lib/i18n.js';

const RESUME_PARTICIPANT = t('swarm.resumeParticipant');
const LOAD_OLDER = t('swarm.loadOlderMessages');

describe('Swarm participant Activity', () => {
  it('opens participant Activity from the Board and detaches its Run silently when leaving', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-live-test'));
    bridge.subscribeRun.mockResolvedValue({
      replay_through_sequence: 0,
      subscription_id: 'subscription-live-test',
    });
    bridge.unsubscribeRun.mockRejectedValue(
      new Error('detached-stream-sentinel'),
    );
    await openSwarm(bridge);
    button('Alpha').click();
    await vi.waitFor(() =>
      expect(document.querySelector('.history')).not.toBeNull(),
    );
    expect(
      document.querySelector('[role="tab"][aria-selected="true"]').textContent,
    ).toContain(t('swarm.tabs.activity'));
    expect(bridge.subscribeRun).toHaveBeenCalledWith('swr-a', 'run-live-test');
    bridge.emitRun('subscription-live-test', {
      type: 'model_step_usage',
      run_id: 'run-live-test',
      sequence: 1,
      payload: { context_usage: { tokens: 2468, estimated: true } },
    });
    await tick();
    expect(document.querySelector('.context-usage').textContent).toContain(
      '~2,468',
    );
    button(t('swarm.newRun')).click();
    await tick();
    expect(bridge.unsubscribeRun).toHaveBeenCalledWith(
      'subscription-live-test',
    );
    expect(document.querySelector('.history')).toBeNull();
    await settle();
    expect(document.body.textContent).not.toContain('detached-stream-sentinel');
  });

  it('opens Activity links through the extension bridge', async () => {
    const { bridge, operation } = createBridge();
    await openParticipant(bridge);
    await tick();
    button(t('swarm.tabs.activity')).click();
    await tick();
    const link = document.querySelector('.history a[href]');
    expect(link).not.toBeNull();
    link.click();
    await tick();
    expect(operation).toHaveBeenCalledWith('link.open', {
      url: 'https://example.test/',
    });
  });

  it('cancels the exact running Bash call and keeps a cancellation failure visible', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-bash'));
    bridge.cancelToolCall.mockRejectedValue(
      new Error('test-owned cancellation failure'),
    );
    bridge.readHistory.mockResolvedValue({ messages: [], status: 'running' });
    bridge.subscribeRun.mockResolvedValue({
      replay_through_sequence: 0,
      subscription_id: 'stream-bash',
    });
    await openParticipant(bridge);
    await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
    bridge.emitRun('stream-bash', {
      type: 'tool_call_started',
      run_id: 'run-bash',
      sequence: 1,
      payload: {
        tool_call: {
          id: 'call-bash',
          name: 'bash',
          arguments: { command: 'test-command' },
        },
      },
    });
    const cancel = t('chat.cancelToolCallAria');
    await vi.waitFor(() => expect(button(cancel)).toBeDefined());
    button(cancel).click();
    await vi.waitFor(() =>
      expect(bridge.cancelToolCall).toHaveBeenCalledWith(
        'swr-a',
        'run-bash',
        'call-bash',
      ),
    );
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain(
        'test-owned cancellation failure',
      ),
    );
  });

  it('shows canonical context and resumes only the selected failed, cancelled or interrupted participant', async () => {
    const stopped = [
      ['prt-b', 'Beta', 'failed', 850],
      ['prt-c', 'Gamma', 'cancelled', 851],
      ['prt-d', 'Delta', 'interrupted', 852],
    ];
    const { bridge, operation } = createBridge(profile, {
      ...structuredClone(swarm),
      participants: [
        { ...swarm.participants[0], state: 'running', run_active: true },
        ...stopped.map(([id, display_name, state]) => ({
          ...swarm.participants[1],
          id,
          display_name,
          state,
          run_active: false,
        })),
      ],
    });
    const tokens = new Map([
      ['prt-a', 120],
      ...stopped.map(([id, , , count]) => [id, count]),
    ]);
    bridge.readHistory.mockImplementation((_swarm, participant) =>
      Promise.resolve({
        messages: [],
        context_usage: {
          tokens: tokens.get(participant),
          estimated: participant !== 'prt-a',
        },
        session_usage: { input_tokens: 99000 },
      }),
    );
    await openSwarm(bridge);
    const context = () =>
      document.querySelector('.context-usage')?.textContent ?? '';
    for (const [id, name, , count] of stopped) {
      button(name).click();
      await vi.waitFor(() => expect(context()).toContain(`~${count}`));
      button(RESUME_PARTICIPANT).click();
      await vi.waitFor(() =>
        expect(operation).toHaveBeenCalledWith(
          'swarms.resume',
          expect.objectContaining({ swarm_id: 'swr-a', participant_id: id }),
        ),
      );
    }
    button('Alpha').click();
    await vi.waitFor(() => expect(context()).toContain('120 / 128,000'));
    expect(button(RESUME_PARTICIPANT)).toBeUndefined();
  });

  it('refreshes pending counts while the participant stays in the same active Run', async () => {
    const { bridge, operation } = createBridge();
    let pendingCount = 12;
    overrideOperations(operation, {
      'swarms.get': () => ({
        swarm: {
          ...structuredClone(swarm),
          participants: swarm.participants.map((participant) => ({
            ...participant,
            lifecycle_run_id: 'run-pending',
            pending_count: participant.id === 'prt-a' ? pendingCount : 0,
          })),
        },
      }),
    });
    await render(bridge);
    button('Investigate').click();
    const pending = () =>
      button('Alpha')?.getAttribute('aria-label') ||
      button('Alpha')?.querySelector('small')?.textContent;
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

  it('renders Provider retries in an empty active participant Session', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-retry'));
    bridge.readHistory.mockResolvedValue({
      messages: [],
      status: 'completed',
    });
    bridge.subscribeRun.mockResolvedValue({
      replay_through_sequence: 0,
      subscription_id: 'retry-sub',
    });
    await openParticipant(bridge);
    await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
    bridge.emitRun('retry-sub', {
      type: 'run_started',
      run_id: 'run-retry',
      sequence: 1,
      payload: { status: 'running' },
    });
    bridge.emitRun('retry-sub', {
      type: 'provider_request_status',
      run_id: 'run-retry',
      sequence: 2,
      payload: {
        state: 'retrying',
        error_kind: 'timeout',
        attempt: 2,
        max_attempts: 4,
      },
    });
    const notice = () => document.querySelector('.run-footer__notice');
    await vi.waitFor(() => expect(notice()).not.toBeNull());
    expect(notice().textContent).toContain('2');
    expect(notice().textContent).toContain('4');
    bridge.emitRun('retry-sub', {
      type: 'assistant_output_delta',
      run_id: 'run-retry',
      sequence: 3,
      payload: { content_delta: 'progress sentinel' },
    });
    await vi.waitFor(() => expect(notice()).toBeNull());
  });

  it('reconciles canonical final output even when completion arrives before the subscription reply', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-final-test'));
    bridge.readHistory.mockResolvedValueOnce({ messages: [] });
    bridge.readHistory.mockResolvedValue({
      runs: [{ run_id: 'run-final-test', status: 'completed', complete: true }],
      messages: [
        {
          history_run_id: 'run-final-test',
          id: 'msg-final-test',
          role: 'assistant',
          content: 'final-output-sentinel',
          timestamp: '2026-09-08T09:00:00+00:00',
        },
        {
          history_run_id: 'run-final-test',
          id: 'summary-final-test',
          role: 'run_summary',
          run_id: 'run-final-test',
          status: 'completed',
        },
      ],
    });
    bridge.subscribeRun.mockImplementation(() => {
      bridge.emitRun('subscription-final-test', {
        type: 'run_completed',
        run_id: 'run-final-test',
        sequence: 1,
      });
      return Promise.resolve({
        replay_through_sequence: 0,
        subscription_id: 'subscription-final-test',
      });
    });
    await openParticipant(bridge);
    await vi.waitFor(() =>
      expect(historyText()).toContain('final-output-sentinel'),
    );
    expect(bridge.readHistory).toHaveBeenCalledTimes(2);
    expect(document.querySelectorAll('.history article')).toHaveLength(1);
    expect(document.querySelector('.history .streaming-text')).toBeNull();
  });
});

describe('Swarm participant History pages', () => {
  it('retains older Session pages during invalidation and reconciles them on completion', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-paged'));
    bridge.subscribeRun.mockResolvedValue({
      replay_through_sequence: 0,
      subscription_id: 'stream-paged',
    });
    const entry = (n) => ({
      id: `history-${n}`,
      role: 'user',
      content: `history-sentinel-${n}`,
      timestamp: '2026-09-09T10:00:00+00:00',
    });
    // Each page names the cursor of the next older page.
    const pages = {
      newest: { first: 4, before: 'middle' },
      middle: { first: 2, before: 'oldest' },
      oldest: { first: 0, before: null },
    };
    bridge.readHistory.mockImplementation(
      async (_swarm, _participant, query) => {
        if (query.after === 'after-six')
          return {
            messages: [entry(6)],
            incremental: true,
            has_newer: false,
            next_after: 'after-seven',
            history_generation: 'generation',
            runs: [],
          };
        const page = pages[query.before ?? 'newest'];
        return {
          messages: [entry(page.first), entry(page.first + 1)],
          history_generation: 'generation',
          next_after: 'after-six',
          runs: [],
          has_more: page.before !== null,
          next_before: page.before,
        };
      },
    );
    await openParticipant(bridge);
    await vi.waitFor(() => expect(button(LOAD_OLDER)).toBeDefined());
    button(LOAD_OLDER).click();
    await vi.waitFor(() =>
      expect(historyText()).toContain('history-sentinel-2'),
    );
    button(LOAD_OLDER).click();
    await vi.waitFor(() =>
      expect(historyText()).toContain('history-sentinel-0'),
    );
    expect(button(LOAD_OLDER)).toBeUndefined();
    expect(bridge.readHistory).toHaveBeenNthCalledWith(2, 'swr-a', 'prt-a', {
      limit: 100,
      before: 'middle',
    });
    expect(bridge.readHistory).toHaveBeenNthCalledWith(3, 'swr-a', 'prt-a', {
      limit: 100,
      before: 'oldest',
    });
    bridge.invalidate();
    await settle(150);
    expect(bridge.readHistory).toHaveBeenCalledTimes(3);
    bridge.emitRun('stream-paged', {
      type: 'run_completed',
      run_id: 'run-paged',
      sequence: 1,
    });
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(4));
    expect(bridge.readHistory).toHaveBeenLastCalledWith('swr-a', 'prt-a', {
      limit: 100,
      after: 'after-six',
    });
    await tick();
    const text = historyText();
    const positions = Array.from({ length: 7 }, (_, n) =>
      text.indexOf(`history-sentinel-${n}`),
    );
    expect(positions.every((value) => value >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
  });

  it('ignores an older Session page that arrives after selecting another participant', async () => {
    const { bridge } = createBridge();
    let finishOlder;
    bridge.readHistory.mockImplementation(async (_swarm, participant, query) =>
      query.before
        ? new Promise((resolve) => {
            finishOlder = resolve;
          })
        : {
            messages: [{ id: participant, role: 'user', content: participant }],
            has_more: participant === 'prt-a',
            next_before: 'older',
          },
    );
    await openParticipant(bridge);
    await vi.waitFor(() => expect(button(LOAD_OLDER)).toBeDefined());
    button(LOAD_OLDER).click();
    await vi.waitFor(() => expect(finishOlder).toBeTypeOf('function'));
    button('Beta').click();
    await vi.waitFor(() => expect(historyText()).toContain('prt-b'));
    finishOlder({
      messages: [
        { id: 'older-alpha', role: 'user', content: 'stale-alpha-sentinel' },
      ],
      has_more: false,
    });
    await settle();
    expect(historyText()).toContain('prt-b');
    expect(historyText()).not.toContain('stale-alpha-sentinel');
  });
});
