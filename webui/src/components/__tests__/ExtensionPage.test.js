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
const { default: ExtensionPageHost } =
  await import('./ExtensionPageHost.svelte');

const descriptor = {
  extension: 'alpha',
  page: 'main',
  epoch: 'epoch-a',
  entry_url: '/asset',
  title: 'Alpha',
};
let component = null;

afterEach(async () => {
  if (component) component = await unmount(component);
  document.body.innerHTML = '';
  operation.mockReset();
  history.mockReset();
  const api = await import('$lib/api.js');
  api.openExtensionPageRun.mockReset();
  api.subscribeRunEvents.mockReset();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function loadFrame() {
  const frame = document.querySelector('iframe');
  const child = frame.contentWindow;
  const sent = vi.spyOn(child, 'postMessage');
  frame.dispatchEvent(new Event('load'));
  const init = sent.mock.calls.find(
    ([data]) => data.type === 'vbot.extension.init',
  )[0];
  return { child, sent, init };
}

function message(child, data) {
  const event = new MessageEvent('message', { data });
  Object.defineProperties(event, {
    origin: { value: 'null' },
    source: { value: child },
  });
  window.dispatchEvent(event);
}

describe('ExtensionPage', () => {
  it.each(['reload', 'unmount'])(
    'ignores pending operation and Run-open replies after frame %s',
    async (transition) => {
      let resolveOperation;
      let resolveRun;
      const api = await import('$lib/api.js');
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
      component = mount(ExtensionPageHost, {
        target: document.body,
        props: { initialDescriptor: descriptor },
      });
      flushSync();
      const { child, sent, init } = loadFrame();
      message(child, { ...init, type: 'vbot.extension.ready' });
      message(child, {
        ...init,
        type: 'vbot.extension.call',
        id: 'pending-operation',
        method: 'operation',
        params: { operation: 'read', arguments: {} },
      });
      message(child, {
        ...init,
        type: 'vbot.extension.call',
        id: 'pending-stream',
        method: 'run.subscribe',
        params: { group_id: 'group', run_id: 'run' },
      });
      expect(operation).toHaveBeenCalledOnce();
      expect(api.openExtensionPageRun).toHaveBeenCalledOnce();
      if (transition === 'reload')
        document.querySelector('iframe').dispatchEvent(new Event('load'));
      else {
        await unmount(component);
        component = null;
      }
      const sentBefore = sent.mock.calls.length;
      resolveOperation({ stale: true });
      resolveRun({ stream: { url: '/api/extension-runs/stale' } });
      await Promise.resolve();
      await Promise.resolve();
      expect(sent.mock.calls).toHaveLength(sentBefore);
      expect(api.subscribeRunEvents).not.toHaveBeenCalled();
    },
  );

  it('invalidates page snapshots when a broken Run stream has no live replacement', async () => {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0.5);
    const api = await import('$lib/api.js');
    api.openExtensionPageRun
      .mockResolvedValueOnce({ stream: { url: '/api/extension-runs/one' } })
      .mockResolvedValueOnce({ stream: null });
    const close = vi.fn();
    api.subscribeRunEvents.mockReturnValue({ close });
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    message(child, { ...init, type: 'vbot.extension.ready' });
    message(child, {
      ...init,
      type: 'vbot.extension.call',
      id: 'sub',
      method: 'run.subscribe',
      params: { group_id: 'group', run_id: 'run', after_sequence: 3 },
    });
    await vi.advanceTimersByTimeAsync(0);
    api.subscribeRunEvents.mock.calls[0][1].onError();
    await vi.advanceTimersByTimeAsync(500);
    expect(close).toHaveBeenCalledOnce();
    expect(api.openExtensionPageRun).toHaveBeenLastCalledWith(
      'alpha',
      { id: 'main', epoch: 'epoch-a' },
      'group',
      'run',
      3,
    );
    expect(sent.mock.calls.map(([data]) => data)).toContainEqual(
      expect.objectContaining({
        type: 'vbot.extension.invalidate',
        nonce: init.nonce,
        reason: 'run_stream_recovered',
      }),
    );
  });

  it('permits clipboard writes from the opaque frame without granting reads or same-origin access', () => {
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const frame = document.querySelector('iframe');
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame.getAttribute('allow')).toBe('clipboard-write *');
  });

  it('waits for the current iframe editor to save and rejects a pending flush on reload', async () => {
    let participant;
    const autosaveContext = {
      register: (value) => {
        participant = value;
        return () => {};
      },
    };
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor, autosaveContext },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    message(child, { ...init, type: 'vbot.extension.ready' });
    message(child, {
      ...init,
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
    expect(participant.hasPending()).toBe(true);
    const pending = participant.flush();
    const request = sent.mock.calls.at(-1)[0];
    message(child, {
      ...request,
      type: 'vbot.extension.autosave.result',
      id: 'wrong',
      saved: true,
    });
    message(child, {
      ...request,
      type: 'vbot.extension.autosave.result',
      saved: true,
    });
    await expect(pending).resolves.toBe(true);
    const interrupted = participant.flush();
    document.querySelector('iframe').dispatchEvent(new Event('load'));
    await expect(interrupted).resolves.toBe(false);
  });

  it.each([
    [128 * 1024, 'vbot.extension.result'],
    [8 * 1024 * 1024, 'vbot.extension.error'],
  ])(
    'settles a catalog reply of %i bytes instead of dropping it',
    async (size, type) => {
      component = mount(ExtensionPageHost, {
        target: document.body,
        props: { initialDescriptor: descriptor },
      });
      flushSync();
      const { child, sent, init } = loadFrame();
      message(child, { ...init, type: 'vbot.extension.ready' });
      const result = { catalog: { description: 'x'.repeat(size) } };
      operation.mockResolvedValue(result);
      message(child, {
        ...init,
        type: 'vbot.extension.call',
        id: 'catalog',
        method: 'operation',
        params: { operation: 'catalog', arguments: {} },
      });
      await Promise.resolve();
      await Promise.resolve();
      const reply = sent.mock.calls
        .map(([data]) => data)
        .find((data) => data.id === 'catalog');
      expect(reply?.type).toBe(type);
      if (type === 'vbot.extension.result')
        expect(reply.result).toEqual(result);
    },
  );

  it('requires matching Ready before it invokes an owner-bound operation', async () => {
    const routeChange = vi.fn();
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: {
        initialDescriptor: descriptor,
        initialContext: {
          locale: 'de',
          timezone: 'Europe/Berlin',
          theme: { mode: 'dark' },
        },
        onRouteChange: routeChange,
      },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    const call = {
      type: 'vbot.extension.call',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
      id: '1',
      method: 'operation',
      params: { operation: 'read', arguments: {} },
    };
    message(child, call);
    expect(operation).not.toHaveBeenCalled();
    message(child, {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
    });
    operation.mockResolvedValue({ ok: true });
    message(child, call);
    await Promise.resolve();
    expect(operation).toHaveBeenCalledWith(
      'alpha',
      'read',
      {},
      { id: 'main', epoch: 'epoch-a' },
    );
    expect(
      sent.mock.calls.some(
        ([data]) =>
          data.type === 'vbot.extension.context' &&
          data.locale === 'de' &&
          data.timezone === 'Europe/Berlin',
      ),
    ).toBe(true);
    message(child, {
      type: 'vbot.extension.call',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
      id: '2',
      method: 'route.replace',
      params: { route: '/details' },
    });
    expect(routeChange).toHaveBeenCalledWith('/details');
  });

  it('rejects a foreign window, malformed envelope, and stale nonce', () => {
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const { child, init } = loadFrame();
    message(window, {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
    });
    message(child, null);
    message(child, {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: 'stale',
      epoch: init.epoch,
      descriptor: init.descriptor,
    });
    expect(operation).not.toHaveBeenCalled();
  });

  it('invalidates immediately on descriptor replacement and forwards display updates', () => {
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    message(child, {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
    });

    component.update({
      locale: 'fr',
      timezone: 'America/New_York',
      theme: { mode: 'light' },
    });
    flushSync();
    expect(
      sent.mock.calls.some(
        ([data]) =>
          data.type === 'vbot.extension.context' &&
          data.locale === 'fr' &&
          data.timezone === 'America/New_York' &&
          data.theme.mode === 'light',
      ),
    ).toBe(true);

    component.update({
      invalidation: { owner: 'other', page: 'main', revision: 1 },
    });
    flushSync();
    expect(
      sent.mock.calls.some(
        ([data]) =>
          data.type === 'vbot.extension.invalidate' && data.revision === 1,
      ),
    ).toBe(false);

    component.update({ descriptor: { ...descriptor, epoch: 'epoch-b' } });
    flushSync();
    expect(
      sent.mock.calls.some(
        ([data]) =>
          data.type === 'vbot.extension.invalidate' &&
          data.reason === 'descriptor_changed' &&
          data.nonce === init.nonce,
      ),
    ).toBe(true);
  });

  it('opens only files projected by its current owner-bound history response', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    message(child, {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
    });
    history.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'report' }],
      file_urls: ['/api/files/capability.signature'],
    });
    const historyCall = {
      type: 'vbot.extension.call',
      version: 1,
      nonce: init.nonce,
      epoch: init.epoch,
      descriptor: init.descriptor,
      id: 'history',
      method: 'history.read',
      params: {
        group_id: 'group-a',
        participant_id: 'participant-a',
        query: { limit: 1 },
      },
    };
    message(child, historyCall);
    await Promise.resolve();
    await Promise.resolve();
    expect(history).toHaveBeenCalledWith(
      'alpha',
      { id: 'main', epoch: 'epoch-a' },
      'group-a',
      'participant-a',
      { limit: 1 },
    );
    expect(
      sent.mock.calls.some(
        ([data]) =>
          data.type === 'vbot.extension.result' && data.id === 'history',
      ),
    ).toBe(true);

    const openCall = (id, url) =>
      message(child, {
        ...historyCall,
        id,
        method: 'media.open',
        params: { url },
      });
    openCall('allowed', '/api/files/capability.signature');
    openCall('forged', '/api/files/forged.signature');
    openCall('script', 'javascript:alert(1)');
    openCall('external', 'https://example.com/report');
    await Promise.resolve();
    await Promise.resolve();

    expect(open).toHaveBeenCalledTimes(2);
    expect(open).toHaveBeenCalledWith(
      window.location.origin + '/api/files/capability.signature',
      '_blank',
      'noopener,noreferrer',
    );
    expect(open).toHaveBeenLastCalledWith(
      'https://example.com/report',
      '_blank',
      'noopener,noreferrer',
    );
    expect(
      sent.mock.calls
        .filter(([data]) => data.type === 'vbot.extension.error')
        .map(([data]) => data.id),
    ).toEqual(expect.arrayContaining(['forged', 'script']));

    document.querySelector('iframe').dispatchEvent(new Event('load'));
    const reloadedInit = sent.mock.calls
      .map(([data]) => data)
      .filter((data) => data.type === 'vbot.extension.init')
      .at(-1);
    message(child, {
      type: 'vbot.extension.ready',
      version: 1,
      nonce: reloadedInit.nonce,
      epoch: reloadedInit.epoch,
      descriptor: reloadedInit.descriptor,
    });
    message(child, {
      ...historyCall,
      nonce: reloadedInit.nonce,
      epoch: reloadedInit.epoch,
      descriptor: reloadedInit.descriptor,
      id: 'expired-file',
      method: 'link.open',
      params: { url: '/api/files/capability.signature' },
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(open).toHaveBeenCalledTimes(2);
    expect(
      sent.mock.calls.some(
        ([data]) =>
          data.type === 'vbot.extension.error' && data.id === 'expired-file',
      ),
    ).toBe(true);
    open.mockRestore();
  });
});

it.each([
  ['assistant_output_delta', { content_delta: 'live-sentinel' }],
  [
    'tool_call_stdout',
    { chunk: 'tool-output-sentinel', tool_call_id: 'tool-a' },
  ],
  ['model_step_usage', { context_usage: { tokens: 2468, estimated: true } }],
])(
  'forwards the actual %s SSE payload to the Extension page',
  async (type, payload) => {
    const mocked = await import('$lib/api.js');
    const actual = await vi.importActual('$lib/api.js');
    let source;
    class ReviewEventSource extends EventTarget {
      constructor() {
        super();
        source = this;
      }
      close() {}
    }
    mocked.openExtensionPageRun.mockResolvedValue({
      stream: { url: '/api/extension-runs/review' },
      replay_through_sequence: 3,
    });
    mocked.subscribeRunEvents.mockImplementation((url, handlers) =>
      actual.subscribeRunEvents(url, handlers, {
        EventSource: ReviewEventSource,
      }),
    );
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    message(child, { ...init, type: 'vbot.extension.ready' });
    message(child, {
      ...init,
      type: 'vbot.extension.call',
      id: 'review-sub',
      method: 'run.subscribe',
      params: { group_id: 'review-group', run_id: 'review-run' },
    });
    await vi.waitFor(() => expect(source).toBeDefined());
    source.dispatchEvent(
      new MessageEvent(type, {
        data: JSON.stringify({
          run_id: 'review-run',
          sequence: 3,
          payload,
        }),
      }),
    );
    await vi.waitFor(() =>
      expect(
        sent.mock.calls
          .map(([value]) => value)
          .find(
            (value) =>
              value.type === 'vbot.extension.result' &&
              value.id === 'review-sub',
          )?.result?.replay_through_sequence,
      ).toBe(3),
    );
    const forwarded = sent.mock.calls
      .map(([value]) => value)
      .find((value) => value.type === 'vbot.extension.stream');
    expect(forwarded.id).toBe('review-sub');
    expect(forwarded.event).toMatchObject({
      type,
      run_id: 'review-run',
      sequence: 3,
      payload,
    });
  },
);

it('keeps file links from every loaded history page and rejects stale participant replies', async () => {
  const open = vi.spyOn(window, 'open').mockImplementation(() => null);
  component = mount(ExtensionPageHost, {
    target: document.body,
    props: { initialDescriptor: descriptor },
  });
  flushSync();
  const { child, sent, init } = loadFrame();
  message(child, { ...init, type: 'vbot.extension.ready' });
  const call = (id, method, params) =>
    message(child, {
      ...init,
      type: 'vbot.extension.call',
      id,
      method,
      params,
    });
  const read = (id, participant, query = {}) =>
    call(id, 'history.read', {
      group_id: 'group-a',
      participant_id: participant,
      query,
    });
  const reply = (id) =>
    vi.waitFor(() =>
      expect(
        sent.mock.calls.some(
          ([data]) => data.type === 'vbot.extension.result' && data.id === id,
        ),
      ).toBe(true),
    );
  for (const [id, query] of [
    ['newest', {}],
    ['oldest', { before: 'older' }],
  ]) {
    history.mockResolvedValueOnce({
      file_urls: [`/api/files/${id}.signature`],
    });
    read(id, 'participant-a', query);
    await reply(id);
  }
  for (const id of ['newest', 'oldest'])
    call(`open-${id}`, 'link.open', { url: `/api/files/${id}.signature` });
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
  await reply('current');
  finishStale({ file_urls: ['/api/files/stale.signature'] });
  await reply('stale');
  for (const id of ['newest', 'oldest', 'stale', 'current'])
    call(`after-switch-${id}`, 'link.open', {
      url: `/api/files/${id}.signature`,
    });
  await reply('after-switch-current');
  expect(open).toHaveBeenCalledTimes(3);
  expect(open).toHaveBeenLastCalledWith(
    window.location.origin + '/api/files/current.signature',
    '_blank',
    'noopener,noreferrer',
  );
  expect(
    sent.mock.calls
      .filter(([data]) => data.type === 'vbot.extension.error')
      .map(([data]) => data.id),
  ).toEqual([
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
    const api = await import('$lib/api.js');
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
    component = mount(ExtensionPageHost, {
      target: document.body,
      props: { initialDescriptor: descriptor },
    });
    flushSync();
    const { child, sent, init } = loadFrame();
    message(child, { ...init, type: 'vbot.extension.ready' });
    const call = async (id, method, params) => {
      message(child, {
        ...init,
        type: 'vbot.extension.call',
        id,
        method,
        params,
      });
      await vi.waitFor(() =>
        expect(sent.mock.calls.some(([data]) => data.id === id)).toBe(true),
      );
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
      '_blank',
      'noopener,noreferrer',
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
    await call('open-late', 'media.open', { url: '/api/files/late.signature' });
    await call('open-old', 'link.open', { url: '/api/files/fresh.signature' });
    expect(open).toHaveBeenCalledOnce();
    expect(
      sent.mock.calls
        .filter(([data]) => data.type === 'vbot.extension.error')
        .map(([data]) => data.id),
    ).toEqual(['open-forged', 'open-late', 'open-old']);
  },
);

it('forwards Tool cancellation only from the current page frame', async () => {
  const api = await import('$lib/api.js');
  component = mount(ExtensionPageHost, {
    target: document.body,
    props: { initialDescriptor: descriptor },
  });
  flushSync();
  const { child, init, sent } = loadFrame();
  message(child, { ...init, type: 'vbot.extension.ready' });
  const request = {
    ...init,
    type: 'vbot.extension.call',
    id: 'cancel-a',
    method: 'run.cancel_tool',
    params: { group_id: 'group-a', run_id: 'run-a', tool_call_id: 'call-a' },
  };
  message(window, request);
  message(child, { ...request, nonce: 'stale' });
  expect(api.cancelExtensionPageToolCall).not.toHaveBeenCalled();
  message(child, request);
  await vi.waitFor(() =>
    expect(api.cancelExtensionPageToolCall).toHaveBeenCalledWith(
      'alpha',
      { id: 'main', epoch: 'epoch-a' },
      'group-a',
      'run-a',
      'call-a',
    ),
  );
  await vi.waitFor(() =>
    expect(
      sent.mock.calls.some(
        ([data]) => data.id === 'cancel-a' && data.result?.ok === true,
      ),
    ).toBe(true),
  );
});
