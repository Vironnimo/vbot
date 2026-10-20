// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import {
  tick,
  profile,
  swarm,
  button,
  headButton,
  notice,
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
import {
  assistantRun,
  messageItem,
  outputItem,
  toolItem,
} from './timelineItems.support.js';

const RESUME_PARTICIPANT = t('swarm.resumeParticipant');
const LOAD_OLDER = t('swarm.loadOlderMessages');
const PICK_PARTICIPANT = t('swarm.activity.empty');

// The compact context figures beside the inspected participant's name.
const contextFigures = () =>
  document.querySelector('.activity-head .context-ring__figures')
    ?.textContent ?? '';

describe('Swarm participant Activity', () => {
  it('opens participant Activity from the Board and detaches its Run silently when leaving', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-live-test'));
    bridge.subscribeRun.mockResolvedValue({
      timeline_version: 1,
      subscription_id: 'subscription-live-test',
    });
    bridge.unsubscribeRun.mockRejectedValue(
      new Error('detached-stream-sentinel'),
    );
    await openSwarm(bridge);
    button('Alpha').click();
    await vi.dynamicImportSettled();
    await vi.waitFor(() =>
      expect(document.querySelector('.history')).not.toBeNull(),
    );
    expect(
      document.querySelector('[role="tab"][aria-selected="true"]').textContent,
    ).toContain(t('swarm.tabs.activity'));
    const panel = document.getElementById('swarm-run-panel-participants');
    expect(panel.getAttribute('role')).toBe('tabpanel');
    expect(panel.getAttribute('aria-labelledby')).toBe(
      'swarm-run-tab-participants',
    );
    expect(bridge.subscribeRun).toHaveBeenCalledWith(
      'swr-a',
      'run-live-test',
      0,
      { timeline: true, childLimit: 100 },
    );
    bridge.emitRun('subscription-live-test', {
      type: 'timeline_snapshot',
      run_id: 'run-live-test',
      sequence: 1,
      items: [assistantRun('run-live-test', [])],
      state: {
        status: 'running',
        context_usage: { tokens: 2468, estimated: true },
      },
    });
    await vi.waitFor(() => expect(contextFigures()).toBe('2.5K / 128K · 2%'));
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
    await vi.waitFor(() =>
      expect(document.querySelector('.history a[href]')).not.toBeNull(),
    );
    const link = document.querySelector('.history a[href]');
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
    bridge.readHistory.mockResolvedValue({
      messages: [],
      items: [],
      status: 'running',
    });
    bridge.subscribeRun.mockResolvedValue({
      timeline_version: 1,
      subscription_id: 'stream-bash',
    });
    await openParticipant(bridge);
    await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
    bridge.emitRun('stream-bash', {
      type: 'timeline_snapshot',
      run_id: 'run-bash',
      sequence: 1,
      items: [
        assistantRun('run-bash', [
          toolItem({
            id: 'call-bash',
            name: 'bash',
            arguments: { command: 'test-command' },
          }),
        ]),
      ],
      state: { status: 'running' },
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

  it('asks for a participant only while none is inspected and shows a chosen one while its History loads', async () => {
    const { bridge } = createBridge();
    let finish;
    bridge.readHistory.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await openSwarm(bridge);
    button(t('swarm.tabs.activity')).click();
    await tick();
    const asking = () => document.body.textContent.includes(PICK_PARTICIPANT);
    expect(asking()).toBe(true);
    button('Alpha').click();
    await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
    expect(asking()).toBe(false);
    expect(
      document.querySelector('.activity-head__name').textContent.trim(),
    ).toBe('Alpha');
    expect(
      document.querySelector('.activity-view [role="status"]').textContent,
    ).toBe(t('common.loading'));
    finish({
      items: [
        messageItem({
          id: 'msg-loaded',
          role: 'user',
          content: 'loaded-sentinel',
          timestamp: '2026-09-08T09:00:00+00:00',
        }),
      ],
      messages: [
        {
          id: 'msg-loaded',
          role: 'user',
          content: 'loaded-sentinel',
          timestamp: '2026-09-08T09:00:00+00:00',
        },
      ],
    });
    await vi.waitFor(() => expect(historyText()).toContain('loaded-sentinel'));
    expect(document.querySelector('.activity-view [role="status"]')).toBeNull();
    button(t('common.close')).click();
    await tick();
    expect(asking()).toBe(true);
  });

  it('says why a stopped participant stopped unless its timeline ends with the error', async () => {
    const entry = (runId, id, role, fields) => ({
      history_run_id: runId,
      id,
      role,
      timestamp: '2026-09-08T09:00:00+00:00',
      ...fields,
    });
    const summary = (runId, status, fields = {}) =>
      entry(runId, `summary-${runId}`, 'run_summary', {
        run_id: runId,
        status,
        ...fields,
      });
    const histories = {
      'prt-a': {
        items: [
          assistantRun('run-net', [outputItem('partial')], {
            source: 'history',
            status: 'interrupted',
            runSummaryMessage: summary('run-net', 'interrupted', {
              completion_reason: 'network',
            }),
          }),
        ],
        runs: [{ run_id: 'run-net', status: 'interrupted', complete: true }],
        messages: [
          entry('run-net', 'out-net', 'assistant', { content: 'partial' }),
          summary('run-net', 'interrupted', { completion_reason: 'network' }),
        ],
      },
      'prt-b': {
        items: [
          assistantRun('run-ok', [outputItem('done')], {
            source: 'history',
            status: 'completed',
          }),
        ],
        runs: [{ run_id: 'run-ok', status: 'completed', complete: true }],
        messages: [
          entry('run-ok', 'out-ok', 'assistant', { content: 'done' }),
          summary('run-ok', 'completed'),
        ],
      },
      'prt-c': {
        items: [
          assistantRun('run-err', [outputItem('trying')], {
            source: 'history',
            status: 'failed',
          }),
          messageItem(
            entry('run-err', 'error-err', 'error', {
              content: 'test-owned Provider failure',
              error_kind: 'provider_error',
            }),
          ),
        ],
        runs: [{ run_id: 'run-err', status: 'failed', complete: true }],
        messages: [
          entry('run-err', 'out-err', 'assistant', { content: 'trying' }),
          entry('run-err', 'error-err', 'error', {
            content: 'test-owned Provider failure',
            error_kind: 'provider_error',
          }),
          summary('run-err', 'failed'),
        ],
      },
    };
    const { bridge } = createBridge(profile, {
      ...structuredClone(swarm),
      participants: [
        // An interrupted Run leaves its participant failed.
        ['prt-a', 'Alpha', 'failed', 'run-net'],
        ['prt-b', 'Beta', 'failed', 'run-ok'],
        ['prt-c', 'Gamma', 'failed', 'run-err'],
      ].map(([id, display_name, state, lifecycle_run_id]) => ({
        ...swarm.participants[0],
        id,
        display_name,
        state,
        lifecycle_run_id,
        run_active: false,
      })),
    });
    bridge.readHistory.mockImplementation((_swarm, participant) =>
      Promise.resolve(structuredClone(histories[participant])),
    );
    await openSwarm(bridge);
    const notice = () =>
      document.querySelector('.activity-stop')?.textContent.trim() ?? null;
    for (const [name, reason] of [
      ['Alpha', t('swarm.activity.stop.network')],
      ['Beta', t('swarm.activity.stop.notStarted')],
    ]) {
      button(name).click();
      await vi.waitFor(() => expect(notice()).toBe(reason));
    }
    button('Gamma').click();
    await vi.waitFor(() =>
      expect(historyText()).toContain('test-owned Provider failure'),
    );
    expect(notice()).toBeNull();
  });

  it('shows the context use and resumes only the selected failed, cancelled or interrupted participant', async () => {
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
          model: 'demo/model',
          state,
          run_active: false,
        })),
      ],
    });
    const tokens = new Map([
      ['prt-a', 120],
      ...stopped.map(([id, , , count]) => [id, count]),
    ]);
    // A Context that names its window fills that one (the window of the
    // Connection that answered), not the catalog's window of the Model.
    const windows = new Map([['prt-c', 272000]]);
    bridge.readHistory.mockImplementation((_swarm, participant) =>
      Promise.resolve({
        messages: [],
        items: [],
        context_usage: {
          tokens: tokens.get(participant),
          estimated: participant !== 'prt-a',
          ...(windows.has(participant)
            ? { context_window: windows.get(participant) }
            : {}),
        },
        session_usage: { input_tokens: 99000 },
      }),
    );
    await openSwarm(bridge);
    for (const [id, name, , count] of stopped) {
      button(name).click();
      const window = windows.has(id) ? '272K' : '128K';
      await vi.waitFor(() =>
        expect(contextFigures()).toMatch(new RegExp(`^${count} / ${window}`)),
      );
      button(RESUME_PARTICIPANT).click();
      await vi.waitFor(() =>
        expect(operation).toHaveBeenCalledWith(
          'swarms.resume',
          expect.objectContaining({ swarm_id: 'swr-a', participant_id: id }),
        ),
      );
    }
    button('Alpha').click();
    await vi.waitFor(() => expect(contextFigures()).toBe('120 / 128K · <1%'));
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
          new RegExp(`running.*${remaining}\\s+pending`, 'i'),
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
      items: [],
      status: 'completed',
    });
    bridge.subscribeRun.mockResolvedValue({
      timeline_version: 1,
      subscription_id: 'retry-sub',
    });
    await openParticipant(bridge);
    await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
    bridge.emitRun('retry-sub', {
      type: 'timeline_snapshot',
      run_id: 'run-retry',
      sequence: 2,
      items: [
        assistantRun('run-retry', [], {
          providerRequestStatus: {
            state: 'retrying',
            error_kind: 'timeout',
            attempt: 2,
            max_attempts: 4,
          },
        }),
      ],
      state: { status: 'running' },
    });
    const notice = () => document.querySelector('.run-footer__notice');
    await vi.waitFor(() => expect(notice()).not.toBeNull());
    expect(notice().textContent).toContain('2');
    expect(notice().textContent).toContain('4');
    bridge.emitRun('retry-sub', {
      type: 'timeline_update',
      run_id: 'run-retry',
      sequence: 3,
      base_sequence: 2,
      run: { id: 'assistant-run-run-retry', providerRequestStatus: null },
      children: [outputItem('progress sentinel', { streaming: true })],
    });
    await vi.waitFor(() => expect(notice()).toBeNull());
  });

  it('reconciles canonical final output even when completion arrives before the subscription reply', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-final-test'));
    bridge.readHistory.mockResolvedValueOnce({ messages: [], items: [] });
    bridge.readHistory.mockResolvedValue({
      runs: [{ run_id: 'run-final-test', status: 'completed', complete: true }],
      items: [
        assistantRun('run-final-test', [outputItem('final-output-sentinel')], {
          source: 'history',
          status: 'completed',
        }),
      ],
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
        type: 'timeline_snapshot',
        run_id: 'run-final-test',
        sequence: 1,
        items: [assistantRun('run-final-test', [], { status: 'completed' })],
        state: { status: 'completed' },
        terminal: true,
      });
      return Promise.resolve({
        timeline_version: 1,
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

  // The History read after a Run's end is the page's own, whether the Run's
  // events or a refresh report the end.
  it.each([
    [
      'its Run ends',
      (bridge) =>
        bridge.emitRun('stream-ends', {
          type: 'timeline_snapshot',
          run_id: 'run-ends',
          sequence: 1,
          items: [assistantRun('run-ends', [], { status: 'completed' })],
          state: { status: 'completed' },
          terminal: true,
        }),
    ],
    [
      'a refresh finds its Run ended',
      (bridge, detail) => {
        detail.participants[0].run_active = false;
        bridge.invalidate();
      },
    ],
  ])(
    "shows a failed History read after %s as a refresh failure beside an action's error until Retry reads again",
    async (_, endRun) => {
      const detail = swarmWithRun('run-ends');
      const { bridge, operation } = createBridge(profile, detail);
      overrideOperations(operation, {
        'swarms.stop': () => {
          throw new Error('test-owned stop failure');
        },
      });
      bridge.readHistory
        .mockResolvedValueOnce({ messages: [], items: [], status: 'running' })
        .mockRejectedValueOnce(new Error('test-owned history failure'))
        .mockResolvedValue({
          runs: [{ run_id: 'run-ends', status: 'completed', complete: true }],
          items: [
            assistantRun('run-ends', [outputItem('final-output-sentinel')], {
              source: 'history',
              status: 'completed',
            }),
          ],
          messages: [
            {
              history_run_id: 'run-ends',
              id: 'msg-ends',
              role: 'assistant',
              content: 'final-output-sentinel',
              timestamp: '2026-09-08T09:00:00+00:00',
            },
          ],
        });
      bridge.subscribeRun.mockResolvedValue({
        timeline_version: 1,
        subscription_id: 'stream-ends',
      });
      await openParticipant(bridge);
      await vi.waitFor(() => expect(bridge.subscribeRun).toHaveBeenCalled());
      headButton(t('swarm.stop')).click();
      await vi.waitFor(() =>
        expect(notice('alert')).toContain('test-owned stop failure'),
      );
      endRun(bridge, detail);
      await vi.waitFor(() =>
        expect(notice('status')).toContain('test-owned history failure'),
      );
      expect(notice('alert')).toContain('test-owned stop failure');
      // Retry is a user action that reads History again.
      button(t('common.retry')).click();
      await vi.waitFor(() =>
        expect(historyText()).toContain('final-output-sentinel'),
      );
      expect(bridge.readHistory).toHaveBeenCalledTimes(3);
      await vi.waitFor(() => expect(notice('status')).toBe(''));
      expect(notice('alert')).toBe('');
    },
  );
});

describe('Swarm participant History pages', () => {
  it("loads a long Run's earlier steps once its history gap comes into reach", async () => {
    const observers = [];
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback) {
          this.callback = callback;
          this.targets = [];
          observers.push(this);
        }
        observe(target) {
          this.targets.push(target);
        }
        unobserve(target) {
          this.targets = this.targets.filter((item) => item !== target);
        }
        disconnect() {
          this.targets = [];
        }
      },
    );
    const reach = (isIntersecting) => {
      const observer = observers.at(-1);
      observer.callback(
        observer.targets.map((target) => ({ target, isIntersecting })),
      );
    };
    const { bridge } = createBridge(profile, swarm);
    bridge.readHistory.mockResolvedValue({
      history_generation: 'generation',
      messages: [],
      items: [
        assistantRun(
          'run-long',
          [
            {
              id: 'assistant-run-run-long-history-gap',
              type: 'history_gap',
              run_id: 'run-long',
              before: 'step-2',
              remaining: 2,
              position: -1,
            },
            outputItem('test-owned latest step', { id: 'step-2', position: 2 }),
          ],
          { source: 'history', status: 'completed', window_start: 2 },
        ),
      ],
    });
    bridge.readRunChildren.mockResolvedValue({
      run_id: 'run-long',
      history_generation: 'generation',
      before: 'step-2',
      has_more: false,
      items: [
        outputItem('test-owned first step', { id: 'step-0', position: 0 }),
        outputItem('test-owned second step', { id: 'step-1', position: 1 }),
      ],
    });
    try {
      await openParticipant(bridge);
      await vi.waitFor(() =>
        expect(historyText()).toContain('test-owned latest step'),
      );
      reach(false);
      expect(bridge.readRunChildren).not.toHaveBeenCalled();
      reach(true);
      reach(true);
      await vi.waitFor(() =>
        expect(historyText()).toContain('test-owned first step'),
      );
      expect(bridge.readRunChildren).toHaveBeenCalledExactlyOnceWith(
        'swr-a',
        'prt-a',
        {
          runId: 'run-long',
          before: 'step-2',
          historyGeneration: 'generation',
          limit: 100,
        },
      );
      const text = historyText();
      const order = ['first', 'second', 'latest'].map((step) =>
        text.indexOf(`test-owned ${step} step`),
      );
      expect(order).toEqual([...order].sort((a, b) => a - b));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('retains older Session pages during invalidation and reconciles them on completion', async () => {
    const { bridge } = createBridge(profile, swarmWithRun('run-paged'));
    bridge.subscribeRun.mockResolvedValue({
      timeline_version: 1,
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
            items: [messageItem(entry(6))],
            incremental: true,
            has_newer: false,
            next_after: 'after-seven',
            history_generation: 'generation',
            runs: [],
          };
        const page = pages[query.before ?? 'newest'];
        return {
          messages: [entry(page.first), entry(page.first + 1)],
          items: [
            messageItem(entry(page.first)),
            messageItem(entry(page.first + 1)),
          ],
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
      child_limit: 100,
      before: 'middle',
    });
    expect(bridge.readHistory).toHaveBeenNthCalledWith(3, 'swr-a', 'prt-a', {
      limit: 100,
      child_limit: 100,
      before: 'oldest',
    });
    bridge.invalidate();
    await settle(150);
    expect(bridge.readHistory).toHaveBeenCalledTimes(3);
    bridge.emitRun('stream-paged', {
      type: 'timeline_snapshot',
      run_id: 'run-paged',
      sequence: 1,
      items: [assistantRun('run-paged', [], { status: 'completed' })],
      state: { status: 'completed' },
      terminal: true,
    });
    await vi.waitFor(() => expect(bridge.readHistory).toHaveBeenCalledTimes(4));
    expect(bridge.readHistory).toHaveBeenLastCalledWith('swr-a', 'prt-a', {
      limit: 100,
      child_limit: 100,
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
            items: [
              messageItem({
                id: participant,
                role: 'user',
                content: participant,
              }),
            ],
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
      items: [
        messageItem({
          id: 'older-alpha',
          role: 'user',
          content: 'stale-alpha-sentinel',
        }),
      ],
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
