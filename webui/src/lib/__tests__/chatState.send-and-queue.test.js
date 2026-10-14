import { describe, expect, it, vi } from 'vitest';
import {
  appendRunEvent,
  ensureSessionState,
  highestContiguousRunEventSequence,
  startRun,
  visibleTimelineItemsForRender,
} from '../chatState.js';
import { ApiClientError } from '../api/transport.js';
import { t } from '../i18n.js';
import {
  countTimelineTextOccurrences,
  deferred,
  setupController,
} from './chatState.support.js';

describe('send and edit admission', () => {
  it('reconciles queued and started send outcomes into Session state', async () => {
    const startChatRun = vi
      .fn()
      .mockResolvedValueOnce({
        queued: true,
        item: { id: 'queued-two', content: 'Later' },
      })
      .mockResolvedValueOnce({
        run_id: 'run-one',
        sse_url: '/events/run-one',
      });
    const { chatState, controller, runStream } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: {
        startChatRun,
        listQueue: vi.fn().mockResolvedValue({
          items: [{ id: 'queued-two', content: 'Later' }],
        }),
      },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    expect(await controller.sendMessage(sessionState, 'Later')).toMatchObject({
      kind: 'queued',
    });
    expect(sessionState.queue).toMatchObject([
      { id: 'queued-two', content: 'Later' },
    ]);

    expect(await controller.sendMessage(sessionState, 'Now')).toMatchObject({
      kind: 'started',
      runId: 'run-one',
    });
    expect(sessionState.currentRun?.runId).toBe('run-one');
    expect(runStream.subscribeToRun).toHaveBeenCalledWith(
      sessionState,
      '/events/run-one',
      { afterSequence: 0 },
    );
  });

  it('creates the Session of a draft with its first send and announces it before the Run attaches', async () => {
    const startChatRun = vi
      .fn()
      .mockResolvedValueOnce({
        command_handled: true,
        output: 'transient',
        reply: 'Status',
        data: { command: 'status', session_id: null },
      })
      .mockResolvedValueOnce({
        run_id: 'run-one',
        session_id: 'created',
        sse_url: '/events/run-one',
      })
      .mockRejectedValueOnce(new Error('provider down'));
    let displayedKey = '';
    const onSessionCreated = vi.fn((sessionState) => {
      displayedKey = sessionState.key;
    });
    const { chatState, controller, runStream } = setupController({
      isDisplayedSession: (agentId, sessionId) =>
        `${agentId}::${sessionId}` === displayedKey,
      onSessionCreated,
      operationOverrides: { startChatRun },
    });
    const draft = { agentAddress: 'builder@vbot', key: 'builder@vbot::~d' };

    // A command that needs no Session leaves the draft in place.
    expect(await controller.sendToNewSession(draft, '/status')).toEqual({
      kind: 'transient',
      reply: 'Status',
      sessionState: null,
    });
    expect(onSessionCreated).not.toHaveBeenCalled();

    const outcome = await controller.sendToNewSession(draft, 'Hello', {
      fileMentions: ['notes.md'],
    });
    expect(startChatRun).toHaveBeenLastCalledWith({
      agent_id: 'builder@vbot',
      new_session: {},
      content: 'Hello',
      file_mentions: ['notes.md'],
    });
    const created = chatState.sessions['builder@vbot::created'];
    expect(onSessionCreated).toHaveBeenCalledWith(created);
    expect(outcome).toEqual({
      kind: 'started',
      runId: 'run-one',
      sessionState: created,
    });
    expect(created.currentRun?.runId).toBe('run-one');
    expect(runStream.subscribeToRun).toHaveBeenCalledWith(
      created,
      '/events/run-one',
      { afterSequence: 0 },
    );

    // Without a Session, a failure reports beside the draft's composer.
    expect(await controller.sendToNewSession(draft, 'Again')).toEqual({
      kind: 'failed',
    });
    expect(chatState.actionError).toBe(`${t('chat.sendError')} provider down`);
  });

  it('attaches send admission errors only to the addressed Session', async () => {
    const startChatRun = vi.fn().mockRejectedValue(new Error('provider down'));
    const { chatState, controller } = setupController({
      operationOverrides: { startChatRun },
    });
    const addressed = ensureSessionState(chatState, 'alpha', 'session-one');
    const other = ensureSessionState(chatState, 'alpha', 'session-two');

    expect(await controller.sendMessage(addressed, 'Hello')).toEqual({
      kind: 'failed',
    });

    expect(addressed.actionError).toBe(`${t('chat.sendError')} provider down`);
    expect(addressed.error).toBeNull();
    expect(addressed.status).toBe('idle');
    expect(other.actionError).toBe('');
    expect(chatState.actionError).toBe('');
  });

  it.each(['run_completed', 'run_failed', 'run_cancelled', 'run_interrupted'])(
    'keeps %s authoritative when the send response arrives late',
    async (type) => {
      const response = deferred();
      const { controller, chatState } = setupController({
        operationOverrides: { startChatRun: vi.fn(() => response.promise) },
      });
      const session = ensureSessionState(chatState, 'alpha', 'session');
      const sending = controller.sendMessage(session, 'Hello');
      appendRunEvent(session, {
        run_id: 'one',
        sequence: 1,
        type: 'run_started',
      });
      appendRunEvent(session, {
        run_id: 'one',
        sequence: 2,
        type: 'assistant_output_delta',
        payload: { content_delta: 'Reply' },
      });
      const terminal = {
        run_id: 'one',
        sequence: 3,
        type,
        payload: { iteration_count: 2 },
      };
      appendRunEvent(session, terminal);
      response.resolve({
        run_id: 'one',
        status: 'running',
        sse_url: '/one',
        events: [],
      });
      await sending;
      appendRunEvent(session, terminal);
      expect(session.status).toBe(type.slice(4));
      expect(session.currentRun.status).toBe(type.slice(4));
      expect(session.currentRun.iterationCount).toBe(2);
      expect(
        countTimelineTextOccurrences(
          visibleTimelineItemsForRender(session),
          'Reply',
        ),
      ).toBe(1);
      controller.destroy();
    },
  );

  it('keeps a successor Run when an older send response arrives', async () => {
    const response = deferred();
    const { controller, chatState, runStream } = setupController({
      operationOverrides: { startChatRun: vi.fn(() => response.promise) },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session');
    const sending = controller.sendMessage(session, 'Hello');
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 1,
      type: 'run_started',
    });
    appendRunEvent(session, {
      run_id: 'one',
      sequence: 2,
      type: 'run_completed',
    });
    appendRunEvent(session, {
      run_id: 'two',
      sequence: 1,
      type: 'run_started',
    });
    appendRunEvent(session, {
      run_id: 'two',
      sequence: 2,
      type: 'assistant_output_delta',
      payload: { content_delta: 'New' },
    });
    response.resolve({
      run_id: 'one',
      status: 'running',
      sse_url: '/one',
      events: [],
    });
    await sending;
    expect(session.currentRun.runId).toBe('two');
    expect(
      countTimelineTextOccurrences(
        visibleTimelineItemsForRender(session),
        'New',
      ),
    ).toBe(1);
    expect(runStream.subscribeToRun).not.toHaveBeenCalled();
    controller.destroy();
  });

  it('restarts from an edited message only after server admission succeeds', async () => {
    const editChatMessage = vi.fn().mockResolvedValue({
      run_id: 'run-edit',
      sse_url: '/events/run-edit',
    });
    const { chatState, controller, runStream } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: { editChatMessage },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.messages = [
      { id: 'kept', role: 'assistant', content: 'Earlier context' },
      { id: 'target', role: 'user', content: 'Original request' },
      { id: 'discarded', role: 'assistant', content: 'Old answer' },
    ];
    sessionState.sessionUsage = { input_tokens: 100, output_tokens: 20 };
    sessionState.contextUsage = { input_tokens: 80 };

    await expect(
      controller.editMessage(sessionState, 'target', 'Edited request'),
    ).resolves.toEqual({ kind: 'started', runId: 'run-edit' });

    expect(editChatMessage).toHaveBeenCalledWith({
      agent_id: 'alpha',
      session_id: 'session-one',
      message_id: 'target',
      content: 'Edited request',
    });
    expect(sessionState.messages).toEqual([
      { id: 'kept', role: 'assistant', content: 'Earlier context' },
    ]);
    expect(sessionState.sessionUsage).toEqual({
      input_tokens: 100,
      output_tokens: 20,
    });
    expect(sessionState.contextUsage).toBeNull();
    expect(sessionState.currentRun?.runId).toBe('run-edit');
    expect(runStream.subscribeToRun).toHaveBeenCalledWith(
      sessionState,
      '/events/run-edit',
      { afterSequence: 0 },
    );
  });

  it('keeps the visible lineage untouched when edit admission fails', async () => {
    const editChatMessage = vi.fn().mockRejectedValue(new Error('busy'));
    const { chatState, controller } = setupController({
      operationOverrides: { editChatMessage },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    const messages = [
      { id: 'target', role: 'user', content: 'Original request' },
      { id: 'answer', role: 'assistant', content: 'Old answer' },
    ];
    sessionState.messages = messages;

    await expect(
      controller.editMessage(sessionState, 'target', 'Edited request'),
    ).resolves.toEqual({ kind: 'failed' });

    expect(sessionState.messages).toBe(messages);
    expect(sessionState.currentRun).toBeNull();
    expect(sessionState.actionError).toBe(`${t('chat.editError')} busy`);
  });

  it('preserves accepted edit events that arrived before its response', async () => {
    const response = deferred();
    const { controller, chatState } = setupController({
      operationOverrides: { editChatMessage: vi.fn(() => response.promise) },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session');
    session.messages = [{ id: 'target', role: 'user', content: 'Old' }];
    const editing = controller.editMessage(session, 'target', 'New');
    appendRunEvent(session, {
      run_id: 'edit',
      sequence: 1,
      type: 'run_started',
    });
    appendRunEvent(session, {
      run_id: 'edit',
      sequence: 2,
      type: 'assistant_output_delta',
      payload: { content_delta: 'Reply' },
    });
    response.resolve({
      run_id: 'edit',
      status: 'running',
      sse_url: '/edit',
      events: [],
    });
    await editing;
    // The replay cursor still covers the Run start that preceded the response.
    expect(highestContiguousRunEventSequence(session)).toBe(2);
    expect(
      countTimelineTextOccurrences(
        visibleTimelineItemsForRender(session),
        'Reply',
      ),
    ).toBe(1);
    expect(session.messages).toEqual([]);
    controller.destroy();
  });

  it('reconciles an accepted edit without replacing its live successor Run', async () => {
    const response = deferred();
    const history = deferred();
    const loadChatHistory = vi.fn(() => history.promise);
    const { controller, chatState, runStream } = setupController({
      isDisplayedSession: () => true,
      operationOverrides: {
        editChatMessage: vi.fn(() => response.promise),
        loadChatHistory,
      },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session');
    session.messages = [{ id: 'target', role: 'user', content: 'Old' }];
    const editing = controller.editMessage(session, 'target', 'New');
    appendRunEvent(session, {
      run_id: 'edit',
      sequence: 1,
      type: 'run_started',
    });
    appendRunEvent(session, {
      run_id: 'edit',
      sequence: 2,
      type: 'run_completed',
    });
    appendRunEvent(session, {
      run_id: 'next',
      sequence: 1,
      type: 'run_started',
    });
    appendRunEvent(session, {
      run_id: 'next',
      sequence: 2,
      type: 'assistant_output_delta',
      payload: { content_delta: 'Successor output' },
    });
    response.resolve({
      run_id: 'edit',
      status: 'running',
      sse_url: '/edit',
      events: [],
    });
    await vi.waitFor(() => expect(loadChatHistory).toHaveBeenCalledOnce());
    expect(session.currentRun.runId).toBe('next');
    expect(
      countTimelineTextOccurrences(
        visibleTimelineItemsForRender(session),
        'Successor output',
      ),
    ).toBe(1);
    history.resolve({
      messages: [{ id: 'replacement', role: 'user', content: 'New' }],
      active_run: { run_id: 'next', status: 'running', sse_url: '/next' },
    });
    await editing;
    expect(session.messages).toMatchObject([{ id: 'replacement' }]);
    expect(session.currentRun.runId).toBe('next');
    expect(runStream.subscribeToRun).not.toHaveBeenCalled();
    expect(runStream.attachRunStream).toHaveBeenCalledWith(
      session,
      expect.objectContaining({ run_id: 'next' }),
    );
    controller.destroy();
  });

  it.each(['send', 'edit'])(
    'does not reopen a hidden Session stream when %s admission returns after navigation',
    async (action) => {
      const response = deferred();
      let displayedSessionId = 'one';
      const { chatState, controller, runStream } = setupController({
        isDisplayedSession: (_agentId, sessionId) =>
          sessionId === displayedSessionId,
        operationOverrides: {
          startChatRun: vi.fn(() => response.promise),
          editChatMessage: vi.fn(() => response.promise),
        },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      session.messages = [{ id: 'target', role: 'user', content: 'Old' }];
      const admission =
        action === 'send'
          ? controller.sendMessage(session, 'New')
          : controller.editMessage(session, 'target', 'New');
      displayedSessionId = 'two';
      response.resolve({ run_id: 'run-one', sse_url: '/events/run-one' });
      await expect(admission).resolves.toEqual({
        kind: 'started',
        runId: 'run-one',
      });
      expect(session.currentRun.runId).toBe('run-one');
      expect(runStream.subscribeToRun).not.toHaveBeenCalled();
    },
  );
});

describe('Queue', () => {
  it.each(['run_started', 'user_message_persisted', 'history-reconciled'])(
    'does not resurrect an item when its send response follows %s',
    async (observed) => {
      const response = deferred();
      const { chatState, controller } = setupController({
        operationOverrides: {
          startChatRun: vi.fn(() => response.promise),
          listQueue: vi.fn().mockResolvedValue({ items: [] }),
        },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      const pending = controller.sendMessage(session, 'Already executed');
      if (observed !== 'history-reconciled') {
        appendRunEvent(session, {
          type: observed,
          run_id: 'run',
          sequence: 1,
          payload: {
            queue_item_id: 'consumed',
            message: { id: 'user', role: 'user', content: 'Already executed' },
          },
        });
      } else {
        session.messages = [
          { id: 'user', role: 'user', content: 'Already executed' },
        ];
      }
      response.resolve({
        queued: true,
        item: { id: 'consumed', content: 'Already executed' },
      });
      expect(await pending).toEqual({ kind: 'queued' });
      expect(session.queue).toEqual([]);
    },
  );
  it('steers into the active Run and reconciles a consumed item without starting another Run', async () => {
    const steerQueueItem = vi.fn().mockResolvedValue({});
    const listQueue = vi.fn().mockResolvedValue({ items: [] });
    const { chatState, controller, runStream } = setupController({
      operationOverrides: { steerQueueItem, listQueue },
    });
    const session = ensureSessionState(chatState, 'coder@project', 'one');
    session.currentRun = { runId: 'r', status: 'running' };
    session.queue = [{ id: 'q', content: 'Correction', steerable: true }];
    expect(await controller.steerQueued(session, 'q')).toBe(true);
    expect(steerQueueItem).toHaveBeenCalledWith('coder@project', 'one', 'q');
    expect(session.queue).toEqual([]);
    expect(session.currentRun.runId).toBe('r');
    expect(runStream.attachRunStream).not.toHaveBeenCalled();
  });

  it('applies each connection snapshot object only once', () => {
    const { controller, runStream } = setupController();
    const snapshot = { active_runs: [] };

    expect(controller.applyConnectionSnapshot(snapshot)).toBe(true);
    expect(controller.applyConnectionSnapshot(snapshot)).toBe(false);
    expect(runStream.applyConnectionSnapshot).toHaveBeenCalledOnce();
  });

  it('re-syncs only held sessions matching a scoped Queue invalidation', async () => {
    const { chatState, controller, listQueue } = setupController({
      isDisplayedSession: (agentId, sessionId) =>
        agentId === 'builder@project-one' && sessionId === 'session-one',
    });
    const target = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );
    const other = ensureSessionState(
      chatState,
      'reviewer@project-one',
      'session-two',
    );
    other.queue = [{ id: 'held', content: 'Held' }];
    const scope = { agentId: 'builder', sessionId: 'session-one' };

    expect(controller.applyQueueInvalidation(scope)).toBe(true);
    expect(controller.applyQueueInvalidation(scope)).toBe(false);
    await vi.waitFor(() => expect(listQueue).toHaveBeenCalledOnce());

    expect(listQueue).toHaveBeenCalledWith(
      'builder@project-one',
      'session-one',
    );
    expect(target.queue).toEqual([
      {
        id: 'queued-one',
        content: 'Next',
        editable: true,
        created_at: null,
        steerable: false,
        steering: false,
      },
    ]);
  });

  it.each([
    {
      change: 'its Run ended',
      refresh: (controller, sessionState) =>
        controller.refreshSessionQueue(sessionState),
    },
    {
      change: 'another window changed it',
      refresh: (controller, sessionState) =>
        controller.applyQueueInvalidation({
          agentId: sessionState.agentId,
          sessionId: sessionState.sessionId,
        }),
    },
  ])(
    'reads the Queue again after $change only where it is shown or holds items',
    async ({ refresh }) => {
      const { chatState, controller, listQueue } = setupController({
        isDisplayedSession: (agentId, sessionId) =>
          agentId === 'alpha' && sessionId === 'shown',
      });
      const shown = ensureSessionState(chatState, 'alpha', 'shown');
      const holding = ensureSessionState(chatState, 'alpha', 'holding');
      holding.queue = [{ id: 'next', content: 'Next' }];
      const background = ensureSessionState(chatState, 'worker', 'background');

      for (const sessionState of [shown, holding, background]) {
        refresh(controller, sessionState);
      }
      await vi.waitFor(() => expect(listQueue).toHaveBeenCalledTimes(2));

      expect(listQueue.mock.calls).toEqual([
        ['alpha', 'shown'],
        ['alpha', 'holding'],
      ]);
      expect(background.queue).toEqual([]);
    },
  );

  it('authoritatively replaces held Queue projections from a connection snapshot', () => {
    const { chatState, controller, onRestartQueueDiscarded } =
      setupController();
    const identitySession = ensureSessionState(
      chatState,
      'alpha',
      'session-one',
    );
    const projectSession = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-two',
    );
    identitySession.queue = [
      { id: 'lost-one', content: 'Lost one' },
      { id: 'lost-two', content: 'Lost two' },
    ];
    projectSession.queue = [{ id: 'kept', content: 'Old text' }];

    controller.applyConnectionSnapshot({
      replay_status: 'epoch_changed',
      active_runs: [],
      queues: [
        {
          project_id: 'project-one',
          agent_id: 'builder',
          session_id: 'session-two',
          items: [{ id: 'kept', content: 'Server text', editable: true }],
        },
      ],
    });

    expect(identitySession.queue).toEqual([]);
    expect(projectSession.queue).toEqual([
      {
        id: 'kept',
        content: 'Server text',
        editable: true,
        created_at: null,
        steerable: false,
        steering: false,
      },
    ]);
    expect(onRestartQueueDiscarded).toHaveBeenCalledWith(2);
  });

  it('does not report Queue loss for a same-epoch replay gap', () => {
    const { chatState, controller, onRestartQueueDiscarded } =
      setupController();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.queue = [{ id: 'stale', content: 'May have run' }];

    controller.applyConnectionSnapshot({
      replay_status: 'gap',
      active_runs: [],
      queues: [],
    });

    expect(sessionState.queue).toEqual([]);
    expect(onRestartQueueDiscarded).not.toHaveBeenCalled();
  });

  it('invalidates a pending Queue read when a newer connection snapshot arrives', async () => {
    const response = deferred();
    const { controller, chatState } = setupController({
      operationOverrides: { listQueue: vi.fn(() => response.promise) },
    });
    const session = ensureSessionState(chatState, 'alpha', 'session');
    const syncing = controller.syncSessionQueue(session);
    controller.applyConnectionSnapshot({ active_runs: [], queues: [] });
    response.resolve({ items: [{ id: 'obsolete', content: 'old' }] });
    await syncing;
    expect(session.queue).toEqual([]);
    controller.destroy();
  });

  it('reports Queue sync failures and closes subscriptions on destroy', async () => {
    const { chatState, controller, listQueue, runStream } = setupController();
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    const error = new Error('offline');
    listQueue.mockRejectedValueOnce(error);

    await controller.syncSessionQueue(sessionState);
    controller.destroy();

    expect(sessionState.actionError).toBe(`${t('queue.syncError')} offline`);
    expect(chatState.actionError).toBe('');
    expect(runStream.closeSubscriptions).toHaveBeenCalledOnce();
  });

  it('ignores an older Queue response that settles after a newer sync', async () => {
    const older = deferred();
    const newer = deferred();
    const listQueue = vi
      .fn()
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);
    const { chatState, controller } = setupController({
      operationOverrides: { listQueue },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');

    const olderSync = controller.syncSessionQueue(sessionState);
    const newerSync = controller.syncSessionQueue(sessionState);
    newer.resolve({
      items: [{ id: 'new', content: 'Newest', editable: true }],
    });
    await newerSync;
    older.resolve({
      items: [{ id: 'old', content: 'Stale', editable: true }],
    });
    await olderSync;

    expect(sessionState.queue).toEqual([
      {
        id: 'new',
        content: 'Newest',
        editable: true,
        created_at: null,
        steerable: false,
        steering: false,
      },
    ]);
  });

  it('returns whether a Queue edit was saved and preserves failed edits', async () => {
    const updateQueueItem = vi
      .fn()
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('offline'));
    const { chatState, controller } = setupController({
      operationOverrides: {
        updateQueueItem,
        listQueue: vi.fn().mockResolvedValue({
          items: [{ id: 'queued-one', content: 'With file', editable: false }],
        }),
      },
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    sessionState.queue = [
      { id: 'queued-one', content: 'Original', editable: true },
    ];

    expect(
      await controller.updateQueued(sessionState, 'queued-one', 'With file', [
        'notes.md',
      ]),
    ).toBe(true);
    expect(sessionState.queue[0]).toMatchObject({
      content: 'With file',
      editable: false,
    });

    expect(
      await controller.updateQueued(sessionState, 'queued-one', 'Unsaved', []),
    ).toBe(false);
    expect(sessionState.queue[0].content).toBe('With file');
    expect(sessionState.actionError).toBe(`${t('queue.editError')} offline`);
  });

  it.each(['invalidation', 'reconnect'])(
    'keeps a newer authoritative Queue from %s when an older edit reply arrives',
    async (snapshot) => {
      const acknowledgement = deferred();
      const reconciliation = deferred();
      let stored = { id: 'queued-one', content: 'Original', editable: true };
      let holdNextRead = false;
      const listQueue = vi.fn(() => {
        if (holdNextRead) {
          holdNextRead = false;
          return reconciliation.promise;
        }
        return Promise.resolve({ items: [structuredClone(stored)] });
      });
      const updateQueueItem = vi.fn((_agent, _session, _id, content) => {
        stored = { ...stored, content };
        return updateQueueItem.mock.calls.length === 1
          ? acknowledgement.promise
          : Promise.resolve({ ok: true });
      });
      const { chatState, controller } = setupController({
        isDisplayedSession: () => true,
        operationOverrides: { updateQueueItem, listQueue },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      session.queue = [structuredClone(stored)];
      const editing = controller.updateQueued(session, stored.id, 'Window A');
      stored = { ...stored, content: 'Window B' };
      if (snapshot === 'invalidation') {
        controller.applyQueueInvalidation({
          agentId: 'alpha',
          sessionId: 'one',
        });
        await Promise.resolve();
      } else {
        controller.applyConnectionSnapshot({
          active_runs: [],
          queues: [{ agent_id: 'alpha', session_id: 'one', items: [stored] }],
        });
      }
      expect(session.queue[0].content).toBe('Window B');

      holdNextRead = true;
      acknowledgement.resolve({ ok: true });
      await Promise.resolve();
      // Even while the replacement read is pending, the old submitted text
      // must never reappear (and seed a later editor with a stale value).
      expect(session.queue[0].content).toBe('Window B');
      reconciliation.resolve({ items: [structuredClone(stored)] });
      expect(await editing).toBe(true);
      expect(session.queue[0].content).toBe('Window B');

      expect(
        await controller.updateQueued(session, stored.id, 'Window B continued'),
      ).toBe(true);
      expect(session.queue[0].content).toBe('Window B continued');
      controller.destroy();
    },
  );

  it('reconciles independent Queue edits whose replies arrive in reverse order', async () => {
    const first = deferred();
    const second = deferred();
    const stored = [
      { id: 'first', content: 'Original first', editable: true },
      { id: 'second', content: 'Original second', editable: true },
    ];
    const { chatState, controller } = setupController({
      operationOverrides: {
        updateQueueItem: vi.fn((_agent, _session, id, content) => {
          stored.find((item) => item.id === id).content = content;
          return (id === 'first' ? first : second).promise;
        }),
        listQueue: vi.fn(() =>
          Promise.resolve({ items: structuredClone(stored) }),
        ),
      },
    });
    const session = ensureSessionState(chatState, 'alpha', 'one');
    session.queue = structuredClone(stored);
    const edits = [
      controller.updateQueued(session, 'first', 'Edited first'),
      controller.updateQueued(session, 'second', 'Edited second'),
    ];
    second.resolve({ ok: true });
    expect(await edits[1]).toBe(true);
    expect(session.queue).toMatchObject(stored);
    first.resolve({ ok: true });
    expect(await edits[0]).toBe(true);
    expect(session.queue).toMatchObject(stored);
    controller.destroy();
  });

  it.each(['edit', 'remove'])(
    'explains a Queue %s rejected because the item is being steered',
    async (mutation) => {
      const steering = Object.assign(new Error('raw server text'), {
        code: 'queue_item_steering',
      });
      const listQueue = vi.fn().mockResolvedValue({
        items: [{ id: 'old', content: 'Original', steering: true }],
      });
      const { chatState, controller } = setupController({
        operationOverrides: {
          listQueue,
          updateQueueItem: vi.fn().mockRejectedValue(steering),
          removeFromQueue: vi.fn().mockRejectedValue(steering),
        },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      session.queue = [{ id: 'old', content: 'Original', editable: true }];

      if (mutation === 'edit') {
        expect(await controller.updateQueued(session, 'old', 'Edited')).toBe(
          false,
        );
      } else {
        await controller.removeQueued(session, 'old');
      }

      expect(session.actionError).toBe(t('queue.steeringLocked'));
      expect(listQueue).toHaveBeenCalledOnce();
      expect(session.queue[0]).toMatchObject({ id: 'old', steering: true });
    },
  );

  it.each(['edit', 'remove', 'enqueue'])(
    'keeps an accepted Queue %s when an earlier list response arrives later',
    async (mutation) => {
      const stale = deferred();
      let edited = false;
      const { chatState, controller } = setupController({
        operationOverrides: {
          listQueue: vi
            .fn()
            .mockReturnValueOnce(stale.promise)
            .mockImplementation(async () => ({
              items: [
                {
                  id: 'old',
                  content: edited ? 'Edited' : 'Original',
                  editable: true,
                },
                { id: 'new', content: 'New queued text', editable: true },
              ],
            })),
          updateQueueItem: vi.fn(async () => {
            edited = true;
            return { ok: true };
          }),
          removeFromQueue: vi.fn().mockResolvedValue({ ok: true }),
          startChatRun: vi.fn().mockResolvedValue({
            queued: true,
            item: { id: 'new', content: 'New queued text', editable: true },
          }),
        },
      });
      const session = ensureSessionState(chatState, 'alpha', 'one');
      const original = { id: 'old', content: 'Original', editable: true };
      session.queue = [structuredClone(original)];
      const pendingSync = controller.syncSessionQueue(session);
      if (mutation === 'edit')
        await controller.updateQueued(session, 'old', 'Edited');
      if (mutation === 'remove') await controller.removeQueued(session, 'old');
      if (mutation === 'enqueue')
        await controller.sendMessage(session, 'New queued text');
      const accepted = structuredClone(session.queue);

      stale.resolve({ items: [original] });
      await pendingSync;

      expect(session.queue).toEqual(accepted);
    },
  );
});

describe('Run controls and cancellation', () => {
  it('submits controls to the captured Session and coalesces clicks', async () => {
    let resolve;
    const controlRun = vi.fn(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const { controller, chatState, runStream } = setupController({
      operationOverrides: { controlRun },
    });
    const session = ensureSessionState(chatState, 'agent@project', 'session');
    startRun(session, { run_id: 'one', status: 'running' });
    const pending = controller.controlRun(session, 'compact');
    await controller.controlRun(session, 'compact');
    expect(controlRun).toHaveBeenCalledTimes(1);
    expect(controlRun).toHaveBeenCalledWith({
      agentId: 'agent@project',
      sessionId: 'session',
      runId: 'one',
      action: 'compact',
      toolCallId: undefined,
    });
    const response = { run_id: 'one', controls: { compaction: 'pending' } };
    resolve(response);
    await pending;
    expect(runStream.mergeRunResponse).toHaveBeenCalledWith(session, response);
    expect(session.pendingRunControls).toEqual({});
    controller.destroy();
  });

  it('merges a terminal cancel response and reconciles durable Tool history', async () => {
    const cancelledRun = {
      run_id: 'run-cancelled',
      status: 'cancelled',
      events: [
        {
          type: 'run_cancelled',
          run_id: 'run-cancelled',
          sequence: 2,
          payload: { status: 'cancelled' },
        },
      ],
    };
    const cancelRun = vi.fn().mockResolvedValue(cancelledRun);
    const loadChatHistory = vi.fn().mockResolvedValue({
      runs: [{ run_id: 'run-cancelled', status: 'cancelled', complete: true }],
      active_run: null,
      messages: [
        {
          id: 'assistant-tools',
          role: 'assistant',
          run_id: 'run-cancelled',
          tool_calls: [
            {
              id: 'call-one',
              name: 'bash',
              arguments: { command: 'first command' },
            },
            {
              id: 'call-two',
              name: 'bash',
              arguments: { command: 'second command' },
            },
          ],
        },
        {
          id: 'run-summary',
          role: 'run_summary',
          run_id: 'run-cancelled',
          status: 'cancelled',
        },
      ],
    });
    const { chatState, controller, runStream } = setupController({
      operationOverrides: { cancelRun, loadChatHistory },
    });
    runStream.mergeRunResponse.mockImplementation((state, run) => {
      for (const event of run.events) {
        appendRunEvent(state, event);
      }
      return true;
    });
    const sessionState = ensureSessionState(chatState, 'alpha', 'session-one');
    startRun(sessionState, {
      run_id: 'run-cancelled',
      status: 'running',
      sse_url: '/api/runs/run-cancelled/events',
    });

    await controller.cancelActiveRun(sessionState);

    expect(cancelRun).toHaveBeenCalledWith('run-cancelled', {
      reason: 'user',
    });
    expect(runStream.mergeRunResponse).toHaveBeenCalledWith(
      sessionState,
      cancelledRun,
    );
    expect(loadChatHistory).toHaveBeenCalledWith({
      agent_id: 'alpha',
      session_id: 'session-one',
      limit: 100,
    });
    expect(sessionState.currentRun).toBeNull();
    expect(visibleTimelineItemsForRender(sessionState)[0].tools).toEqual([
      expect.objectContaining({
        toolCallId: 'call-one',
        status: 'cancelled',
      }),
      expect.objectContaining({
        toolCallId: 'call-two',
        status: 'cancelled',
      }),
    ]);
    expect(sessionState.cancellingRunIds).toEqual([]);
  });

  it('keeps pending cancellation on its exact Session and Run across navigation and successors', async () => {
    const first = deferred();
    const second = deferred();
    const successor = deferred();
    const cancelRun = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(successor.promise);
    const { chatState, controller } = setupController({
      operationOverrides: { cancelRun },
    });
    const a = ensureSessionState(chatState, 'alpha', 'one');
    const b = ensureSessionState(chatState, 'beta', 'two');
    a.currentRun = { runId: 'a', status: 'running' };
    b.currentRun = { runId: 'b', status: 'running' };
    const cancellingA = controller.cancelActiveRun(a);
    await controller.cancelActiveRun(a);
    expect(cancelRun).toHaveBeenCalledTimes(1);
    expect(a.cancellingRunIds).toEqual(['a']);
    expect(b.cancellingRunIds).toEqual([]);
    const cancellingB = controller.cancelActiveRun(b);
    a.currentRun = { runId: 'a-next', status: 'running' };
    expect(a.cancellingRunIds).not.toContain('a-next');
    const cancellingNext = controller.cancelActiveRun(a);
    first.reject(new Error('old cancellation failed'));
    await cancellingA;
    expect(a.actionError).toBe('');
    expect(a.cancellingRunIds).toEqual(['a-next']);
    expect(b.cancellingRunIds).toEqual(['b']);
    second.reject(new Error('cancel b failed'));
    successor.reject(new Error('cancel successor failed'));
    await Promise.all([cancellingB, cancellingNext]);
    expect(a.cancellingRunIds).toEqual([]);
    expect(b.cancellingRunIds).toEqual([]);
    expect(cancelRun.mock.calls.map(([id]) => id)).toEqual([
      'a',
      'b',
      'a-next',
    ]);
  });

  const running = { status: 'running', stillRunning: false };
  const lingering = { status: 'completed', stillRunning: true };
  it.each([
    [
      'shows the status it reports at once',
      running,
      () => Promise.resolve({ stopped: true, status: 'stopped' }),
      { status: 'stopped', stillRunning: false },
      '',
    ],
    [
      'shows that nothing of an ended command runs any longer',
      lingering,
      () => Promise.resolve({ stopped: true, status: 'completed' }),
      { status: 'completed', stillRunning: false },
      '',
    ],
    [
      'shows a command the server no longer knows as ended',
      running,
      () =>
        Promise.reject(
          new ApiClientError('command_not_found', 'Unknown command'),
        ),
      { status: 'ended', stillRunning: false },
      '',
    ],
    [
      'reports a failed stop and keeps the known state',
      lingering,
      () => Promise.reject(new ApiClientError('network_error', 'Offline')),
      lingering,
      `${t('chat.cancelBackgroundTaskError')} Offline`,
    ],
  ])(
    'stops a background command by its command id and %s',
    async (_label, known, reply, shown, actionError) => {
      const stopCommand = vi.fn(reply);
      const { chatState, controller } = setupController({
        operationOverrides: { stopCommand },
      });
      const sessionState = ensureSessionState(
        chatState,
        'builder@project-one',
        'session-one',
      );
      controller.applyCommandStatuses({
        cmd_one: { ...known, sessionId: 'session-one' },
      });

      await expect(
        controller.stopCommand({ sessionState, commandId: 'cmd_one' }),
      ).resolves.toBe(!actionError);

      expect(stopCommand).toHaveBeenCalledWith('session-one', 'cmd_one');
      expect(chatState.commandStatuses).toEqual({
        'session-one': { cmd_one: shown },
      });
      expect(sessionState.actionError).toBe(actionError);
    },
  );

  it("reads a command's output, follows its state and tells when the command is gone", async () => {
    const loadCommandOutput = vi
      .fn()
      .mockResolvedValueOnce({
        status: 'completed',
        still_running: true,
        output: 'done',
      })
      .mockRejectedValueOnce(
        new ApiClientError('command_not_found', 'Unknown command'),
      )
      .mockRejectedValueOnce(new ApiClientError('network_error', 'Offline'));
    const { chatState, controller } = setupController({
      operationOverrides: { loadCommandOutput },
    });
    const sessionState = ensureSessionState(
      chatState,
      'builder@project-one',
      'session-one',
    );
    const read = (commandId) =>
      controller.loadCommandOutput({ sessionState, commandId });

    await expect(read('cmd_one')).resolves.toEqual({
      ok: true,
      status: 'completed',
      stillRunning: true,
      output: 'done',
    });
    expect(loadCommandOutput).toHaveBeenCalledWith('session-one', 'cmd_one');
    await expect(read('cmd_two')).resolves.toEqual({
      ok: false,
      error: 'Unknown command',
      gone: true,
    });
    await expect(read('cmd_three')).resolves.toEqual({
      ok: false,
      error: 'Offline',
      gone: false,
    });
    expect(chatState.commandStatuses).toEqual({
      'session-one': {
        cmd_one: { status: 'completed', stillRunning: true },
        cmd_two: { status: 'ended', stillRunning: false },
      },
    });
  });
});
