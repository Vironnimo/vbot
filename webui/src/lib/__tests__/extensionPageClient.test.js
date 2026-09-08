// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

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

function initialize(target, nonce = 'nonce-a', epoch = 'epoch-a') {
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
  });
}

afterEach(() => {
  client?.dispose();
  client = null;
});

describe('extension page client', () => {
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

  it('rejects pending calls on reload and ignores stale results', async () => {
    const target = parent();
    client = createExtensionPageClient({ target });
    initialize(target);
    const pending = client.operation('read', {});
    const oldCall = target.postMessage.mock.calls.at(-1)[0];

    initialize(target, 'nonce-b', 'epoch-b');
    await expect(pending).rejects.toThrow('reloaded');

    dispatchFrom(target, {
      type: 'vbot.extension.result',
      ...oldCall,
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
