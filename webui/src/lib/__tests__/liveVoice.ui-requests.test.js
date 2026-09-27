import { describe, expect, it } from 'vitest';

import { deferred, flush, liveFixture } from './liveVoice.support.js';

describe('Live voice UI requests', () => {
  it('returns the App context and answers each request once', async () => {
    const f = liveFixture();
    await f.goLive();
    f.request('r1', 'context');
    f.request('r1', 'context');
    await flush();
    expect(f.uiActions.context).toHaveBeenCalledOnce();
    expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
      'call-1',
      'r1',
      { result: { view: 'chat', agents: [] } },
    );
  });

  it('opens a view alone or exactly one item and reports whether navigation applied', async () => {
    const f = liveFixture();
    await f.goLive();
    f.uiActions.open
      .mockResolvedValueOnce(true)
      .mockResolvedValueOnce(false)
      .mockResolvedValue(true);
    f.request('r1', 'open', {
      view: 'chat',
      agent_id: 'joel@vbot',
      session_id: 's1',
    });
    f.request('r2', 'open', { view: 'terminals', agent_id: null });
    f.request('r3', 'open', { view: 'agents', agent_id: 'joel' });
    f.request('r4', 'open', {
      view: 'projects',
      project_id: 'vbot',
      agent_id: null,
    });
    await flush();

    expect(f.uiActions.open.mock.calls).toEqual([
      [
        { view: 'chat', agent_id: 'joel@vbot', session_id: 's1' },
        { isCurrent: expect.any(Function) },
      ],
      [{ view: 'terminals' }, { isCurrent: expect.any(Function) }],
      [
        { view: 'agents', agent_id: 'joel' },
        { isCurrent: expect.any(Function) },
      ],
      [
        { view: 'projects', project_id: 'vbot' },
        { isCurrent: expect.any(Function) },
      ],
    ]);
    expect(f.api.sendLiveUiResult).toHaveBeenCalledWith('call-1', 'r1', {
      result: { applied: true },
    });
    expect(f.api.sendLiveUiResult).toHaveBeenCalledWith('call-1', 'r2', {
      result: { applied: false },
    });
  });

  it.each([
    ['open', { view: 'settings' }, 'invalid_view'],
    [
      'open',
      { view: 'agents', agent_id: 'joel', session_id: 's1' },
      'invalid_arguments',
    ],
    ['open', { view: 'agents', project_id: 'vbot' }, 'invalid_arguments'],
    ['open', { view: 'projects', project_id: '' }, 'invalid_arguments'],
    [
      'open',
      { view: 'terminals', agent_id: 'joel', session_id: 's1' },
      'invalid_arguments',
    ],
    ['open', { view: 'chat', agent_id: 'joel' }, 'invalid_arguments'],
    ['terminal_view', { op: 'close', terminal_id: 't1' }, 'invalid_arguments'],
    ['terminal_view', { op: 'show' }, 'invalid_arguments'],
    ['terminal_view', { op: 'show_group' }, 'invalid_arguments'],
    ['context', ['unexpected'], 'invalid_arguments'],
    ['send_message', {}, 'unsupported_action'],
  ])(
    'answers an invalid %s request %j with %s without running it',
    async (action, args, code) => {
      const f = liveFixture();
      await f.goLive();
      f.request('bad', action, args);
      await flush();
      expect(f.uiActions.context).not.toHaveBeenCalled();
      expect(f.uiActions.open).not.toHaveBeenCalled();
      expect(f.uiActions.terminalView).not.toHaveBeenCalled();
      expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
        'call-1',
        'bad',
        { error: code },
      );
      expect(f.onNotice).not.toHaveBeenCalled();
    },
  );

  it('runs Terminal layout requests with only their target fields', async () => {
    const f = liveFixture();
    await f.goLive();
    f.request('r1', 'terminal_view', {
      op: 'maximize',
      terminal_id: 't1',
      group_id: 'ignored',
    });
    f.request('r2', 'terminal_view', { op: 'show_group', group_id: 'g1' });
    f.request('r3', 'terminal_view', { op: 'refresh' });
    await flush();
    expect(
      f.uiActions.terminalView.mock.calls.map(([target]) => target),
    ).toEqual([
      { op: 'maximize', terminal_id: 't1' },
      { op: 'show_group', group_id: 'g1' },
      { op: 'refresh' },
    ]);
    expect(f.api.sendLiveUiResult).toHaveBeenCalledWith('call-1', 'r1', {
      result: { visible_order: ['t1'] },
    });
  });

  it.each([
    [new Error('terminal_not_found'), 'terminal_not_found'],
    [
      Object.assign(new Error('Navigation was cancelled'), {
        code: 'navigation_not_applied',
      }),
      'navigation_not_applied',
    ],
    [new Error('Cannot read properties of undefined'), 'operation_failed'],
  ])(
    'answers a failed UI action with its error code: %s',
    async (error, code) => {
      const f = liveFixture();
      await f.goLive();
      f.uiActions.terminalView.mockRejectedValue(error);
      f.request('r1', 'terminal_view', { op: 'show', terminal_id: 't9' });
      await flush();
      expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
        'call-1',
        'r1',
        { error: code },
      );
      expect(f.onNotice).toHaveBeenCalledExactlyOnceWith({
        code: 'ui_action_failed',
        severity: 'warn',
      });
    },
  );

  it('drops the result of a request whose call has stopped', async () => {
    const f = liveFixture();
    await f.goLive();
    const pending = deferred();
    f.uiActions.context.mockReturnValue(pending.promise);
    f.request('r1', 'context');
    await flush();
    const [guard] = f.uiActions.context.mock.calls[0];
    expect(guard.isCurrent()).toBe(true);
    f.controller.stop();
    expect(guard.isCurrent()).toBe(false);
    f.frame({ type: 'closed', reason: 'stopped', usage: null });
    pending.resolve({ view: 'chat' });
    await flush();
    expect(f.api.sendLiveUiResult).not.toHaveBeenCalled();
  });

  it('declines requests that arrive while the call is closing', async () => {
    const f = liveFixture();
    await f.goLive();
    f.controller.stop();
    f.request('r1', 'context');
    await flush();
    expect(f.uiActions.context).not.toHaveBeenCalled();
    expect(f.api.sendLiveUiResult).toHaveBeenCalledExactlyOnceWith(
      'call-1',
      'r1',
      { error: 'call_closing' },
    );
  });
});
