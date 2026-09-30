// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);

const operation = vi.fn();
const history = vi.fn();
vi.mock('$lib/api.js', () => ({
  invokeExtensionPageOperation: (...args) => operation(...args),
  openExtensionPageRun: vi.fn(),
  cancelExtensionPageToolCall: vi.fn().mockResolvedValue({ ok: true }),
  readExtensionPageHistory: (...args) => history(...args),
  subscribeRunEvents: vi.fn(),
}));
const api = await import('$lib/api.js');
const { createStandaloneNavigation } =
  await import('$lib/navigation.svelte.js');
const { default: ExtensionPageHost } =
  await import('./ExtensionPageHost.support.svelte');

const descriptor = {
  extension: 'alpha',
  page: 'main',
  epoch: 'epoch-a',
  entry_url: '/asset',
  title: 'Alpha',
};
const pageRef = { id: 'main', epoch: 'epoch-a' };
const OPEN_OPTIONS = ['_blank', 'noopener,noreferrer'];
let component = null;

afterEach(async () => {
  if (component) component = await unmount(component);
  document.body.innerHTML = '';
  operation.mockReset();
  history.mockReset();
  api.openExtensionPageRun.mockReset();
  api.subscribeRunEvents.mockReset();
  api.cancelExtensionPageToolCall.mockClear();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// Mounts the host and loads its frame; returns the frame bridge.
function mountPage(props = {}) {
  component = mount(ExtensionPageHost, {
    target: document.body,
    props: { initialDescriptor: descriptor, ...props },
  });
  flushSync();
  return loadFrame();
}

function loadFrame() {
  const frame = document.querySelector('iframe');
  const child = frame.contentWindow;
  const sent = vi.spyOn(child, 'postMessage');
  frame.dispatchEvent(new Event('load'));
  const posted = () => sent.mock.calls.map(([data]) => data);
  const init = posted().findLast((data) => data.type === 'vbot.extension.init');
  const page = {
    child,
    sent,
    init,
    posted,
    message: (data) => message(child, data),
    ready: () => message(child, { ...init, type: 'vbot.extension.ready' }),
    call: (id, method, params) =>
      message(child, {
        ...init,
        type: 'vbot.extension.call',
        id,
        method,
        params,
      }),
    reply: (id) =>
      posted().find(
        (data) =>
          data.id === id &&
          ['vbot.extension.result', 'vbot.extension.error'].includes(data.type),
      ),
    replied: (id) => vi.waitFor(() => expect(page.reply(id)).toBeDefined()),
    errors: () =>
      posted()
        .filter((data) => data.type === 'vbot.extension.error')
        .map((data) => data.id),
  };
  return page;
}

// Mounts the host and completes the frame handshake. Ready arrives after the
// host has rendered the loaded frame, as a real frame's does.
function openPage(props) {
  const page = mountPage(props);
  flushSync();
  page.ready();
  return page;
}

function message(source, data) {
  const event = new MessageEvent('message', { data });
  Object.defineProperties(event, {
    origin: { value: 'null' },
    source: { value: source },
  });
  window.dispatchEvent(event);
}

async function flushReplies() {
  await Promise.resolve();
  await Promise.resolve();
}

describe('ExtensionPage frame bridge', () => {
  it('permits clipboard writes from the opaque frame without granting reads or same-origin access', () => {
    mountPage();
    const frame = document.querySelector('iframe');
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame.getAttribute('allow')).toBe('clipboard-write *');
  });

  it('accepts calls only after Ready from the current frame and publishes the display context', async () => {
    const page = mountPage({
      initialContext: {
        locale: 'de',
        timezone: 'Europe/Berlin',
        theme: { mode: 'dark' },
      },
    });
    const ready = { ...page.init, type: 'vbot.extension.ready' };
    message(window, ready);
    page.message(null);
    page.message({ ...ready, nonce: 'stale' });
    page.call('1', 'operation', { operation: 'read', arguments: {} });
    expect(operation).not.toHaveBeenCalled();
    expect(
      page.posted().some((data) => data.type === 'vbot.extension.context'),
    ).toBe(false);
    page.ready();
    operation.mockResolvedValue({ ok: true });
    page.call('1', 'operation', { operation: 'read', arguments: {} });
    await Promise.resolve();
    expect(operation).toHaveBeenCalledWith('alpha', 'read', {}, pageRef);
    expect(page.posted()).toContainEqual(
      expect.objectContaining({
        type: 'vbot.extension.context',
        locale: 'de',
        timezone: 'Europe/Berlin',
      }),
    );
  });

  it('spells the navigation place as the page route and moves it on route calls', async () => {
    const navigation = createStandaloneNavigation(['swarms', 'swr-a']);
    const navigate = vi.spyOn(navigation, 'navigate');
    const replace = vi.spyOn(navigation, 'replace');
    const page = openPage({ navigation });
    const routes = () =>
      page
        .posted()
        .filter((data) => data.type === 'vbot.extension.context')
        .map((data) => data.route);
    expect(page.init.route).toBe('/swarms/swr-a');
    // Back/Forward and the main navigation move the place.
    navigation.replace([]);
    flushSync();
    expect(routes().at(-1)).toBe('');
    page.call('push', 'route.push', { route: '/swarms/swr-b/usage' });
    page.call('replace', 'route.replace', { route: '/profiles/prf-a' });
    await page.replied('replace');
    expect(navigate).toHaveBeenCalledWith(['swarms', 'swr-b', 'usage']);
    expect(replace).toHaveBeenLastCalledWith(['profiles', 'prf-a']);
    expect(page.reply('push').result).toEqual({});
    flushSync();
    expect(routes().at(-1)).toBe('/profiles/prf-a');
  });

  it('invalidates immediately on descriptor replacement and forwards display updates', () => {
    const page = openPage();
    component.update({
      locale: 'fr',
      timezone: 'America/New_York',
      theme: { mode: 'light' },
    });
    flushSync();
    expect(page.posted()).toContainEqual(
      expect.objectContaining({
        type: 'vbot.extension.context',
        locale: 'fr',
        timezone: 'America/New_York',
        theme: { mode: 'light' },
      }),
    );
    component.update({ descriptor: { ...descriptor, epoch: 'epoch-b' } });
    flushSync();
    expect(page.posted()).toContainEqual(
      expect.objectContaining({
        type: 'vbot.extension.invalidate',
        reason: 'descriptor_changed',
        nonce: page.init.nonce,
      }),
    );
  });

  it("forwards an Extension's changes only to its own page", () => {
    const page = openPage();
    const invalidations = () =>
      page.posted().filter((data) => data.type === 'vbot.extension.invalidate');
    const change = { resource: 'swarms', ids: ['swarm-one'], revision: 4 };
    component.invalidate({ owner: 'other', change, revision: 1 });
    expect(invalidations()).toEqual([]);
    component.invalidate({ owner: 'alpha', change, revision: 2 });
    // After an Extension reload every open page refreshes everything.
    component.invalidate({ owner: null, change: null, revision: 3 });
    // A change the bridge cannot carry still refreshes the page.
    component.invalidate({
      owner: 'alpha',
      change: { resource: 'swarms', ids: [7], revision: 5 },
      revision: 4,
    });
    expect(invalidations()).toEqual([
      expect.objectContaining({
        nonce: page.init.nonce,
        epoch: 'epoch-a',
        revision: 2,
        change,
      }),
      expect.not.objectContaining({ change: expect.anything() }),
      expect.not.objectContaining({ change: expect.anything() }),
    ]);
  });

  it.each([
    [
      'reload',
      () => document.querySelector('iframe').dispatchEvent(new Event('load')),
    ],
    [
      'unmount',
      async () => {
        await unmount(component);
        component = null;
      },
    ],
  ])(
    'ignores pending operation and Run-open replies after frame %s',
    async (_transition, leave) => {
      let resolveOperation;
      let resolveRun;
      operation.mockReturnValue(
        new Promise((resolve) => {
          resolveOperation = resolve;
        }),
      );
      api.openExtensionPageRun.mockReturnValue(
        new Promise((resolve) => {
          resolveRun = resolve;
        }),
      );
      const page = openPage();
      page.call('pending-operation', 'operation', {
        operation: 'read',
        arguments: {},
      });
      page.call('pending-stream', 'run.subscribe', {
        group_id: 'group',
        run_id: 'run',
      });
      expect(operation).toHaveBeenCalledOnce();
      expect(api.openExtensionPageRun).toHaveBeenCalledOnce();
      await leave();
      const sentBefore = page.sent.mock.calls.length;
      resolveOperation({ stale: true });
      resolveRun({ stream: { url: '/api/extension-runs/stale' } });
      await flushReplies();
      expect(page.sent.mock.calls).toHaveLength(sentBefore);
      expect(api.subscribeRunEvents).not.toHaveBeenCalled();
    },
  );

  it('waits for the current iframe editor to save and rejects a pending flush on reload', async () => {
    let participant;
    const autosaveContext = {
      register: (value) => {
        participant = value;
        return () => {};
      },
    };
    const page = openPage({ autosaveContext });
    page.message({
      ...page.init,
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
    expect(participant.hasPending()).toBe(true);
    const pending = participant.flush();
    const request = page.posted().at(-1);
    page.message({
      ...request,
      type: 'vbot.extension.autosave.result',
      id: 'wrong',
      saved: true,
    });
    page.message({
      ...request,
      type: 'vbot.extension.autosave.result',
      saved: true,
    });
    await expect(pending).resolves.toBe(true);
    const interrupted = participant.flush();
    document.querySelector('iframe').dispatchEvent(new Event('load'));
    await expect(interrupted).resolves.toBe(false);
  });

  it('stops sharing a released flush: the next transition asks the frame again and the left flush still settles', async () => {
    let participant;
    const autosaveContext = {
      register: (value) => {
        participant = value;
        return () => {};
      },
    };
    const page = openPage({ autosaveContext });
    page.message({
      ...page.init,
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
    const left = participant.flush();
    const leftRequest = page.posted().at(-1);

    participant.release();
    expect(page.posted().at(-1)).toMatchObject({
      type: 'vbot.extension.autosave.release',
      nonce: page.init.nonce,
      epoch: page.init.epoch,
    });
    const next = participant.flush();
    const nextRequest = page.posted().at(-1);
    expect(nextRequest.type).toBe('vbot.extension.autosave.flush');
    expect(nextRequest.id).not.toBe(leftRequest.id);

    page.message({
      ...nextRequest,
      type: 'vbot.extension.autosave.result',
      saved: true,
    });
    await expect(next).resolves.toBe(true);
    page.message({
      ...leftRequest,
      type: 'vbot.extension.autosave.result',
      saved: true,
    });
    await expect(left).resolves.toBe(true);
  });

  it("holds one App layer while the current frame shows dialogs and lets Back close the page's topmost one", () => {
    const layers = [];
    const shell = {
      registerLayer: (layer) => {
        layers.push(layer);
        return () => layers.splice(layers.indexOf(layer), 1);
      },
    };
    const page = openPage({ shell });
    const report = (open, fields = {}) =>
      page.message({
        ...page.init,
        type: 'vbot.extension.layers.state',
        open,
        ...fields,
      });
    report(true, { nonce: 'stale' });
    expect(layers).toHaveLength(0);
    report(true);
    report(true);
    expect(layers).toHaveLength(1);
    layers[0].close();
    expect(page.posted().at(-1)).toEqual({
      type: 'vbot.extension.layers.close',
      version: 1,
      nonce: page.init.nonce,
      epoch: 'epoch-a',
      descriptor: page.init.descriptor,
    });
    report(false);
    expect(layers).toHaveLength(0);
    report(true);
    // A reloaded page starts without layers.
    loadFrame();
    expect(layers).toHaveLength(0);
  });

  it.each([
    ['the Desktop app', true, 1],
    ['a browser', false, 0],
  ])(
    'moves the App history for Back/Forward input forwarded from the current frame in %s',
    (_app, handlesInput, moves) => {
      const shell = {
        handlesInput,
        back: vi.fn(),
        forward: vi.fn(),
        registerLayer: () => () => {},
      };
      const page = mountPage({ shell });
      expect(page.init.forwardHistoryInput).toBe(handlesInput);
      const move = (direction, fields = {}) => ({
        ...page.init,
        type: 'vbot.extension.history.move',
        direction,
        ...fields,
      });
      page.message(move('back'));
      page.ready();
      message(window, move('back'));
      page.message(move('back', { nonce: 'stale' }));
      page.message(move('up'));
      page.message(move('back'));
      page.message(move('forward'));
      expect(shell.back).toHaveBeenCalledTimes(moves);
      expect(shell.forward).toHaveBeenCalledTimes(moves);
    },
  );

  it.each([
    [128 * 1024, (result) => ({ type: 'vbot.extension.result', result })],
    [8 * 1024 * 1024, () => ({ type: 'vbot.extension.error' })],
  ])(
    'settles a catalog reply of %i bytes instead of dropping it',
    async (size, expected) => {
      const page = openPage();
      const result = { catalog: { description: 'x'.repeat(size) } };
      operation.mockResolvedValue(result);
      page.call('catalog', 'operation', {
        operation: 'catalog',
        arguments: {},
      });
      await flushReplies();
      expect(page.reply('catalog')).toMatchObject(expected(result));
    },
  );

  it('forwards Tool cancellation only from the current page frame', async () => {
    const page = openPage();
    const request = {
      ...page.init,
      type: 'vbot.extension.call',
      id: 'cancel-a',
      method: 'run.cancel_tool',
      params: { group_id: 'group-a', run_id: 'run-a', tool_call_id: 'call-a' },
    };
    message(window, request);
    page.message({ ...request, nonce: 'stale' });
    expect(api.cancelExtensionPageToolCall).not.toHaveBeenCalled();
    page.message(request);
    await page.replied('cancel-a');
    expect(api.cancelExtensionPageToolCall).toHaveBeenCalledWith(
      'alpha',
      pageRef,
      'group-a',
      'run-a',
      'call-a',
    );
    expect(page.reply('cancel-a').result).toEqual({ ok: true });
  });
});

describe('ExtensionPage file links', () => {
  it('opens only files projected by its current owner-bound history response', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const page = openPage();
    history.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'report' }],
      file_urls: ['/api/files/capability.signature'],
    });
    const historyParams = {
      group_id: 'group-a',
      participant_id: 'participant-a',
      query: { limit: 1 },
    };
    page.call('history', 'history.read', historyParams);
    await flushReplies();
    expect(history).toHaveBeenCalledWith(
      'alpha',
      pageRef,
      'group-a',
      'participant-a',
      { limit: 1 },
    );
    expect(page.reply('history').type).toBe('vbot.extension.result');

    for (const [id, url] of [
      ['allowed', '/api/files/capability.signature'],
      ['forged', '/api/files/forged.signature'],
      ['script', 'javascript:alert(1)'],
      ['external', 'https://example.com/report'],
    ])
      page.call(id, 'media.open', { url });
    await flushReplies();
    expect(open.mock.calls).toEqual([
      [
        window.location.origin + '/api/files/capability.signature',
        ...OPEN_OPTIONS,
      ],
      ['https://example.com/report', ...OPEN_OPTIONS],
    ]);
    expect(page.errors()).toEqual(['forged', 'script']);

    const reloaded = loadFrame();
    reloaded.ready();
    reloaded.call('expired-file', 'link.open', {
      url: '/api/files/capability.signature',
    });
    await flushReplies();
    expect(open).toHaveBeenCalledTimes(2);
    expect(reloaded.errors()).toContain('expired-file');
  });

  it('keeps file links from every loaded history page and rejects stale participant replies', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const page = openPage();
    const read = (id, participant, query = {}) =>
      page.call(id, 'history.read', {
        group_id: 'group-a',
        participant_id: participant,
        query,
      });
    for (const [id, query] of [
      ['newest', {}],
      ['oldest', { before: 'older' }],
    ]) {
      history.mockResolvedValueOnce({
        file_urls: [`/api/files/${id}.signature`],
      });
      read(id, 'participant-a', query);
      await page.replied(id);
    }
    for (const id of ['newest', 'oldest'])
      page.call(`open-${id}`, 'link.open', {
        url: `/api/files/${id}.signature`,
      });
    expect(open).toHaveBeenCalledTimes(2);

    let finishStale;
    history.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishStale = resolve;
        }),
    );
    read('stale', 'participant-a');
    history.mockResolvedValueOnce({
      file_urls: ['/api/files/current.signature'],
    });
    read('current', 'participant-b');
    await page.replied('current');
    finishStale({ file_urls: ['/api/files/stale.signature'] });
    await page.replied('stale');
    for (const id of ['newest', 'oldest', 'stale', 'current'])
      page.call(`after-switch-${id}`, 'link.open', {
        url: `/api/files/${id}.signature`,
      });
    await page.replied('after-switch-current');
    expect(open).toHaveBeenCalledTimes(3);
    expect(open).toHaveBeenLastCalledWith(
      window.location.origin + '/api/files/current.signature',
      ...OPEN_OPTIONS,
    );
    expect(page.errors()).toEqual([
      'after-switch-newest',
      'after-switch-oldest',
      'after-switch-stale',
    ]);
  });

  it.each([
    ['group-a', 'participant-b'],
    ['group-b', 'participant-a'],
  ])(
    'opens streamed file links only for the selected history scope (switch to %s/%s)',
    async (groupId, participantId) => {
      const open = vi.spyOn(window, 'open').mockImplementation(() => null);
      let transport;
      api.openExtensionPageRun.mockResolvedValue({
        stream: { url: '/api/extension-runs/current' },
        participant_id: 'participant-a',
        replay_through_sequence: 0,
      });
      api.subscribeRunEvents.mockImplementation((_url, handlers) => {
        transport = handlers;
        return { close: vi.fn() };
      });
      history.mockResolvedValue({ messages: [], file_urls: [] });
      const page = openPage();
      const call = async (id, method, params) => {
        page.call(id, method, params);
        await page.replied(id);
      };
      await call('history-a', 'history.read', {
        group_id: 'group-a',
        participant_id: 'participant-a',
      });
      await call('stream-a', 'run.subscribe', {
        group_id: 'group-a',
        run_id: 'run-a',
      });
      const stream = (sequence, token) =>
        transport.onEvent({
          type: 'assistant_output',
          data: {
            run_id: 'run-a',
            sequence,
            payload: {
              message: {
                content: `[result](/api/files/${token}.signature) [unverified](/api/files/forged.signature)`,
              },
            },
            file_urls: [`/api/files/${token}.signature`],
          },
        });
      stream(1, 'fresh');
      await call('open-fresh', 'link.open', {
        url: '/api/files/fresh.signature',
      });
      expect(open).toHaveBeenCalledExactlyOnceWith(
        `${window.location.origin}/api/files/fresh.signature`,
        ...OPEN_OPTIONS,
      );
      expect(history).toHaveBeenCalledOnce();
      await call('open-forged', 'link.open', {
        url: '/api/files/forged.signature',
      });
      await call('history-next', 'history.read', {
        group_id: groupId,
        participant_id: participantId,
      });
      stream(2, 'late');
      await call('open-late', 'media.open', {
        url: '/api/files/late.signature',
      });
      await call('open-old', 'link.open', {
        url: '/api/files/fresh.signature',
      });
      expect(open).toHaveBeenCalledOnce();
      expect(page.errors()).toEqual(['open-forged', 'open-late', 'open-old']);
    },
  );
});

describe('ExtensionPage Run streams', () => {
  it('forwards the canonical events of the API subscriber to the Extension page', async () => {
    const actual = await vi.importActual('$lib/api.js');
    let source;
    class ReviewEventSource extends EventTarget {
      constructor() {
        super();
        source = this;
      }
      close() {}
    }
    api.openExtensionPageRun.mockResolvedValue({
      stream: { url: '/api/extension-runs/review' },
      replay_through_sequence: 3,
    });
    api.subscribeRunEvents.mockImplementation((url, handlers) =>
      actual.subscribeRunEvents(url, handlers, {
        EventSource: ReviewEventSource,
      }),
    );
    const page = openPage();
    page.call('review-sub', 'run.subscribe', {
      group_id: 'review-group',
      run_id: 'review-run',
    });
    await page.replied('review-sub');
    expect(page.reply('review-sub').result).toMatchObject({
      live: true,
      subscription_id: 'review-sub',
      replay_through_sequence: 3,
    });
    const events = [
      ['assistant_output_delta', { content_delta: 'live-sentinel' }],
      [
        'tool_call_stdout',
        { chunk: 'tool-output-sentinel', tool_call_id: 'tool-a' },
      ],
      [
        'model_step_usage',
        { context_usage: { tokens: 2468, estimated: true } },
      ],
    ];
    for (const [index, [type, payload]] of events.entries())
      source.dispatchEvent(
        new MessageEvent(type, {
          data: JSON.stringify({
            run_id: 'review-run',
            sequence: index + 3,
            payload,
          }),
        }),
      );
    const forwarded = page
      .posted()
      .filter((data) => data.type === 'vbot.extension.stream');
    expect(forwarded.map((data) => data.id)).toEqual([
      'review-sub',
      'review-sub',
      'review-sub',
    ]);
    expect(forwarded.map((data) => data.event)).toEqual(
      events.map(([type, payload], index) => ({
        type,
        run_id: 'review-run',
        sequence: index + 3,
        payload,
      })),
    );
  });
});

describe('ExtensionPage Run stream recovery', () => {
  const initial = { stream: { url: '/api/extension-runs/initial' } };

  // Subscribes a ready page after `afterSequence`; the test drives each
  // transport the host opens through `streams`.
  async function subscribe(afterSequence = 4) {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    const streams = [];
    api.subscribeRunEvents.mockImplementation((url, handlers, options) => {
      const stream = { url, handlers, options, close: vi.fn() };
      streams.push(stream);
      return stream;
    });
    api.openExtensionPageRun.mockResolvedValueOnce(initial);
    api.openExtensionPageRun.mockResolvedValue({
      stream: { url: '/api/extension-runs/fresh' },
    });
    const page = openPage();
    page.call('sub', 'run.subscribe', {
      group_id: 'group',
      run_id: 'run',
      after_sequence: afterSequence,
    });
    await vi.advanceTimersByTimeAsync(0);
    return {
      page,
      streams,
      opens: () => api.openExtensionPageRun.mock.calls.map((call) => call[4]),
      forwarded: () =>
        page
          .posted()
          .filter((data) => data.type === 'vbot.extension.stream')
          .map((data) => data.event.sequence),
      resyncs: () =>
        page
          .posted()
          .filter(
            (data) =>
              data.type === 'vbot.extension.invalidate' &&
              data.reason === 'run_stream_recovered' &&
              data.nonce === page.init.nonce,
          ).length,
    };
  }
  const deliver = (stream, type, sequence) =>
    stream.handlers.onEvent({ type, data: { run_id: 'run', sequence } });

  it('reports a transport that cannot start without leaving a watchdog behind', async () => {
    vi.useFakeTimers();
    api.openExtensionPageRun.mockResolvedValue(initial);
    api.subscribeRunEvents.mockImplementation(() => {
      throw new Error('EventSource unavailable');
    });
    const page = openPage();
    page.call('sub', 'run.subscribe', { group_id: 'group', run_id: 'run' });
    await vi.advanceTimersByTimeAsync(0);
    expect(page.reply('sub')).toMatchObject({
      type: 'vbot.extension.error',
      error: 'EventSource unavailable',
    });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('reopens after the last delivered sequence and ignores retired transports', async () => {
    const { streams, opens, forwarded, resyncs } = await subscribe();
    deliver(streams[0], 'assistant_output_delta', 5);
    streams[0].handlers.onError();
    expect(streams[0].close).toHaveBeenCalledOnce();
    deliver(streams[0], 'run_completed', 6);
    await vi.advanceTimersByTimeAsync(500);
    expect(api.openExtensionPageRun).toHaveBeenLastCalledWith(
      'alpha',
      pageRef,
      'group',
      'run',
      5,
    );
    expect(streams[1].options.afterSequence).toBe(5);
    expect(resyncs()).toBe(1);
    deliver(streams[1], 'assistant_output_delta', 5);
    deliver(streams[1], 'run_completed', 6);
    expect(forwarded()).toEqual([5, 6]);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(opens()).toEqual([4, 5]);
  });

  it('treats heartbeats as liveness and recovers a silently stalled stream', async () => {
    const { streams, opens } = await subscribe();
    await vi.advanceTimersByTimeAsync(20_000);
    streams[0].handlers.onHeartbeat();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(opens()).toEqual([4]);
    await vi.advanceTimersByTimeAsync(5_500);
    expect(opens()).toEqual([4, 4]);
  });

  it('invalidates page snapshots once when the Run has no replay stream left', async () => {
    const { streams, opens, resyncs } = await subscribe();
    api.openExtensionPageRun.mockResolvedValue({ stream: null });
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(streams[0].close).toHaveBeenCalledOnce();
    expect(resyncs()).toBe(1);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(opens()).toEqual([4, 4]);
    expect(streams).toHaveLength(1);
  });

  it('retries a failed capability refresh with backoff', async () => {
    const { streams, opens } = await subscribe();
    api.openExtensionPageRun.mockRejectedValueOnce(new Error('offline'));
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(opens()).toHaveLength(2);
    expect(streams).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(opens()).toHaveLength(3);
    expect(streams).toHaveLength(2);
  });

  it('stops recovery after unsubscribe, including a capability refresh in flight', async () => {
    const { page, streams, opens, resyncs } = await subscribe();
    let resolve;
    api.openExtensionPageRun.mockReturnValue(
      new Promise((finish) => {
        resolve = finish;
      }),
    );
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(opens()).toHaveLength(2);
    page.call('unsub', 'run.unsubscribe', { id: 'sub' });
    resolve({ stream: { url: '/api/extension-runs/late' } });
    await vi.advanceTimersByTimeAsync(0);
    streams[0].handlers.onError();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(opens()).toHaveLength(2);
    expect(streams).toHaveLength(1);
    expect(resyncs()).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
