// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { createAutosaveParticipant } from '../autosave.js';
import { createExtensionPageClient } from '../extensionPageClient.js';

const descriptor = { owner: 'alpha', page: 'main' };
let client = null;

function parent() {
  return { postMessage: vi.fn() };
}

function dispatchFrom(source, data) {
  const event = new MessageEvent('message', { data });
  Object.defineProperty(event, 'source', { value: source });
  window.dispatchEvent(event);
}

function initialize(target, nonce = 'nonce-a', epoch = 'epoch-a', fields = {}) {
  dispatchFrom(target, {
    type: 'vbot.extension.init',
    version: 1,
    nonce,
    epoch,
    descriptor,
    route: '/initial',
    theme: { mode: 'dark' },
    locale: 'de',
    timezone: 'Europe/Berlin',
    ...fields,
  });
}

afterEach(() => {
  client?.dispose();
  client = null;
  vi.useRealTimers();
});

describe('extension page client', () => {
  it('flushes a registered editor and rejects obsolete completion after reinitialization', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    let complete;
    const participant = {
      hasPending: () => true,
      flush: vi.fn(() => new Promise((resolve) => (complete = resolve))),
    };
    client.registerAutosave(participant);
    expect(target.postMessage.mock.calls.at(-1)[0]).toMatchObject({
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
    const request = {
      type: 'vbot.extension.autosave.flush',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
      id: 'flush-a',
    };
    dispatchFrom(target, request);
    await Promise.resolve();
    initialize(target, 'nonce-b', 'epoch-b');
    complete(true);
    await Promise.resolve();
    await Promise.resolve();
    expect(
      target.postMessage.mock.calls.some(
        ([data]) => data.type === 'vbot.extension.autosave.result',
      ),
    ).toBe(false);
    participant.flush.mockResolvedValue(false);
    dispatchFrom(target, {
      ...request,
      id: 'flush-b',
      nonce: 'nonce-b',
      epoch: 'epoch-b',
    });
    await vi.waitFor(() =>
      expect(target.postMessage.mock.calls.at(-1)[0]).toMatchObject({
        type: 'vbot.extension.autosave.result',
        id: 'flush-b',
        saved: false,
      }),
    );
  });

  it('releases the running write of the registered editor and reports edits made after it', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    let draft = 'first';
    const participant = createAutosaveParticipant({
      getSnapshot: () => draft,
      hasChanges: () => draft !== '',
      save: () => new Promise(() => {}),
    });
    client.registerAutosave(participant);
    void participant.runSave();
    const lastMessage = () => target.postMessage.mock.calls.at(-1)[0];

    dispatchFrom(target, {
      type: 'vbot.extension.autosave.release',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
    });
    expect(lastMessage()).toMatchObject({
      type: 'vbot.extension.autosave.state',
      pending: false,
    });

    draft = 'second';
    client.notifyAutosave();
    expect(lastMessage()).toMatchObject({
      type: 'vbot.extension.autosave.state',
      pending: true,
    });
  });

  it('accepts catalog replies larger than the command limit', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const pending = client.operation('catalog');
    const call = target.postMessage.mock.calls.at(-1)[0];
    const result = { catalog: { description: 'x'.repeat(128 * 1024) } };
    dispatchFrom(target, { ...call, type: 'vbot.extension.result', result });
    await expect(pending).resolves.toEqual(result);
  });

  it('rejects a missing reply and allows a fresh request without replaying it', async () => {
    vi.useFakeTimers();
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const failed = expect(client.operation('catalog')).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(30_000);
    await failed;
    const pending = client.operation('catalog');
    const call = target.postMessage.mock.calls.at(-1)[0];
    dispatchFrom(target, {
      ...call,
      type: 'vbot.extension.result',
      result: {},
    });
    await expect(pending).resolves.toEqual({});
    expect(vi.getTimerCount()).toBe(0);
    expect(
      target.postMessage.mock.calls.filter(
        ([data]) => data.type === 'vbot.extension.call',
      ),
    ).toHaveLength(2);
  });

  it('accepts only a versioned matching init and publishes its display context', () => {
    const target = parent();
    const contexts = vi.fn();
    client = createExtensionPageClient({ target });
    client.onContext(contexts);

    dispatchFrom(target, { type: 'vbot.extension.init', version: 2 });
    expect(target.postMessage).not.toHaveBeenCalled();

    initialize(target);

    expect(target.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'vbot.extension.ready',
        version: 1,
        nonce: 'nonce-a',
        epoch: 'epoch-a',
        descriptor,
      }),
      '*',
    );
    expect(client.context).toMatchObject({
      route: '/initial',
      theme: { mode: 'dark' },
      locale: 'de',
      timezone: 'Europe/Berlin',
    });
    expect(contexts).toHaveBeenCalledOnce();
  });

  it.each([
    ['pushRoute', 'route.push'],
    ['replaceRoute', 'route.replace'],
  ])('sends %s to the host as a %s call', (name, method) => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    void client[name]('/swarms/swr-a').catch(() => {});
    expect(target.postMessage.mock.calls.at(-1)[0]).toMatchObject({
      type: 'vbot.extension.call',
      method,
      params: { route: '/swarms/swr-a' },
    });
  });

  it('reports whether layers are open and closes the topmost one when the host asks', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    const states = () =>
      target.postMessage.mock.calls
        .map(([data]) => data)
        .filter((data) => data.type === 'vbot.extension.layers.state')
        .map((data) => data.open);
    const dialog = { close: vi.fn() };
    const menu = { close: vi.fn() };
    const releaseDialog = client.registerLayer(dialog);
    // A layer opened before the host is known is reported with Ready.
    initialize(target);
    const releaseMenu = client.registerLayer(menu);
    dispatchFrom(target, {
      type: 'vbot.extension.layers.close',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
    });
    expect(menu.close).toHaveBeenCalledOnce();
    expect(dialog.close).not.toHaveBeenCalled();
    releaseMenu();
    releaseDialog();
    expect(states()).toEqual([true, false]);
  });

  it('forwards Back/Forward keys and side buttons to the host only when it asks', () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    const moves = () =>
      target.postMessage.mock.calls
        .map(([data]) => data)
        .filter((data) => data.type === 'vbot.extension.history.move')
        .map((data) => [data.nonce, data.direction]);
    // Each returns whether the page's native handling was cancelled.
    const press = (key, modifiers = {}) =>
      !window.dispatchEvent(
        new KeyboardEvent('keydown', { key, cancelable: true, ...modifiers }),
      );
    const click = (type, button) =>
      !window.dispatchEvent(new MouseEvent(type, { button, cancelable: true }));
    initialize(target);
    expect([press('ArrowLeft', { altKey: true }), click('mouseup', 3)]).toEqual(
      [false, false],
    );
    initialize(target, 'nonce-b', 'epoch-b', { forwardHistoryInput: true });
    expect([
      press('ArrowLeft', { altKey: true }),
      press('ArrowRight', { altKey: true, shiftKey: true }),
      press('BrowserForward'),
      click('mousedown', 3),
      click('mouseup', 3),
      click('mouseup', 4),
    ]).toEqual([true, false, true, true, true, true]);
    // A key the page handled itself stays with the page.
    const handled = new KeyboardEvent('keydown', {
      key: 'ArrowLeft',
      altKey: true,
      cancelable: true,
    });
    handled.preventDefault();
    window.dispatchEvent(handled);
    expect(moves()).toEqual([
      ['nonce-b', 'back'],
      ['nonce-b', 'forward'],
      ['nonce-b', 'back'],
      ['nonce-b', 'forward'],
    ]);
    client.dispose();
    expect(press('ArrowLeft', { altKey: true })).toBe(false);
  });

  it('rejects pending calls on reload and ignores stale results', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const pending = client.operation('read', {});
    const oldCall = target.postMessage.mock.calls.at(-1)[0];

    initialize(target, 'nonce-b', 'epoch-b');
    await expect(pending).rejects.toThrow('reloaded');

    dispatchFrom(target, {
      ...oldCall,
      type: 'vbot.extension.result',
      result: { stale: true },
    });
    const current = client.operation('read', {});
    const currentCall = target.postMessage.mock.calls.at(-1)[0];
    dispatchFrom(target, {
      type: 'vbot.extension.result',
      version: 1,
      nonce: currentCall.nonce,
      epoch: currentCall.epoch,
      descriptor,
      id: currentCall.id,
      result: { current: true },
    });

    await expect(current).resolves.toEqual({ current: true });
  });

  it('passes the reason and the changed records of each invalidation to listeners', () => {
    const target = parent();
    const invalidations = vi.fn();
    client = createExtensionPageClient({ target });
    client.onInvalidation(invalidations);
    initialize(target);
    const invalidate = (fields) =>
      dispatchFrom(target, {
        type: 'vbot.extension.invalidate',
        version: 1,
        nonce: 'nonce-a',
        epoch: 'epoch-a',
        descriptor,
        ...fields,
      });
    const change = { resource: 'swarms', ids: ['swarm-one'], revision: 3 };
    invalidate({ revision: 4, change });
    invalidate({ reason: 'run_stream_recovered' });
    // A malformed change is dropped, so the page refreshes everything.
    invalidate({ change: { ...change, ids: 'swarm-one' } });
    expect(invalidations.mock.calls.map(([value]) => value)).toEqual([
      { reason: null, change },
      { reason: 'run_stream_recovered', change: null },
      { reason: null, change: null },
    ]);
  });

  it('cleans up pending work and listeners when the page is disposed', async () => {
    const target = parent();
    const invalidations = vi.fn();
    client = createExtensionPageClient({ target });
    client.onInvalidation(invalidations);
    initialize(target);
    const pending = client.replaceRoute('/next');

    client.dispose();
    await expect(pending).rejects.toThrow('disposed');
    dispatchFrom(target, {
      type: 'vbot.extension.invalidate',
      version: 1,
      nonce: 'nonce-a',
      epoch: 'epoch-a',
      descriptor,
    });
    expect(invalidations).not.toHaveBeenCalled();
  });

  it('does not accept messages from a different window or oversized calls', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(parent());
    expect(client.context).toBeNull();
    initialize(target);
    await expect(
      client.operation('read', { text: 'x'.repeat(64 * 1024) }),
    ).rejects.toThrow('too large');
  });
});
