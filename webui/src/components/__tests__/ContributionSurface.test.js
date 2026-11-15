// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
const invoke = vi.hoisted(() => vi.fn());
vi.mock('$lib/api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  invokeExtensionContribution: (...args) => invoke(...args),
}));

const { default: ContributionSurface } =
  await import('../extensions/ContributionSurface.svelte');
const { default: ContributionIcon } =
  await import('../extensions/ContributionIcon.svelte');
const { default: ContributionContextHost } =
  await import('./ContributionContextHost.support.svelte');

const descriptor = {
  id: 'extension:computer:control',
  extension: 'computer',
  epoch: 'epoch-a',
  surface: 'chat.composer.actions',
  kind: 'action',
  title: 'Computer control',
  operations: ['read', 'stop'],
  source: {
    operation: 'read',
    resource: 'control',
    params: { session_id: { context: 'session_id' } },
  },
};
const stop = {
  items: [
    {
      id: 'stop',
      text: 'Stop computer',
      action: { operation: 'stop', params: { lease_id: 'lease-1' } },
    },
  ],
};
const idle = { items: [{ id: 'idle', text: 'Computer idle' }] };
let instance;
let listeners;
let notifyError;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

async function settle() {
  for (let index = 0; index < 5; index++) {
    await Promise.resolve();
    flushSync();
  }
}

function mountSurface() {
  const source = reactiveProps({
    entries: [descriptor],
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    notifyError,
  });
  const props = reactiveProps({
    surface: descriptor.surface,
    context: { session_id: 'first' },
  });
  instance = mount(ContributionContextHost, {
    target: document.body,
    props: { component: ContributionSurface, source, instances: [props] },
  });
  flushSync();
  return { source, props };
}

function invalidate(owner = 'computer', resource = 'control') {
  for (const listener of listeners) listener({ owner, change: { resource } });
}
const stopButton = () =>
  document.querySelector('button[aria-label="Stop computer"]');

beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = '';
  init('en');
  invoke.mockReset();
  listeners = new Set();
  notifyError = vi.fn();
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  expect(listeners.size).toBe(0);
});

describe('ContributionSurface operations', () => {
  it('renders every public named icon and validated custom geometry', () => {
    instance = mount(ContributionContextHost, {
      target: document.body,
      props: {
        component: ContributionIcon,
        instances: [
          'network',
          'panel',
          'grid',
          'sparkles',
          'factory',
          { paths: Array(32).fill('M0 0L2 2'), view_box: '0 0 4 4' },
          { paths: ['<script>bad</script>'] },
        ].map((icon) => ({ icon })),
      },
    });
    flushSync();
    const icons = [...document.querySelectorAll('svg')];
    expect(icons).toHaveLength(6);
    expect(icons.every((icon) => icon.querySelector('path,rect,circle'))).toBe(
      true,
    );
    expect(icons.at(-1).getAttribute('viewBox')).toBe('0 0 4 4');
    expect(icons.at(-1).querySelectorAll('path')).toHaveLength(32);
    expect(document.querySelector('script')).toBeNull();
  });

  it('filters invalidations and coalesces a burst into one trailing source read', async () => {
    const first = deferred();
    const next = deferred();
    invoke.mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
    mountSurface();
    expect(invoke).toHaveBeenCalledExactlyOnceWith(descriptor, 'read', {
      session_id: 'first',
    });
    invalidate('other-owner');
    invalidate('computer', 'other-resource');
    invalidate();
    invalidate();
    invalidate();
    expect(invoke).toHaveBeenCalledTimes(1);
    first.resolve(stop);
    await settle();
    expect(stopButton()).not.toBeNull();
    expect(invoke).toHaveBeenCalledTimes(2);
    next.resolve(idle);
    await settle();
    expect(document.body.textContent).toContain('Computer idle');
    expect(stopButton()).toBeNull();
    invalidate('other-owner');
    invalidate('computer', 'other-resource');
    await settle();
    expect(invoke).toHaveBeenCalledTimes(2);
  });

  it('retains Stop after a read failure and refreshes after its declared action', async () => {
    invoke
      .mockResolvedValueOnce(stop)
      .mockRejectedValueOnce(new Error('Temporarily unavailable'))
      .mockResolvedValueOnce({ stopped: true })
      .mockResolvedValueOnce(idle);
    mountSurface();
    await settle();
    invalidate();
    await settle();
    expect(stopButton()).not.toBeNull();
    expect(stopButton().disabled).toBe(false);
    stopButton().click();
    await settle();
    expect(invoke.mock.calls.map(([, operation]) => operation)).toEqual([
      'read',
      'read',
      'stop',
      'read',
    ]);
    expect(invoke.mock.calls[2]).toEqual([
      descriptor,
      'stop',
      { lease_id: 'lease-1' },
    ]);
    expect(document.body.textContent).toContain('Computer idle');
    expect(stopButton()).toBeNull();
    expect(notifyError).not.toHaveBeenCalled();
  });

  it('matches custom Resource sources locally or across owners, only within the current epoch', async () => {
    invoke.mockResolvedValue(idle);
    const { source } = mountSurface();
    await settle();
    const publish = (owner = 'computer', overrides = {}) => {
      for (const listener of listeners)
        listener({
          owner,
          change: {
            resource: `extension:${owner}:control`,
            ids: [],
            revision: 1,
          },
          serviceResource: {
            owner,
            epoch: 'epoch-a',
            local_resource: 'control',
            services: [],
            ...overrides,
          },
        });
    };
    publish('other');
    publish('computer', { epoch: 'retired-epoch' });
    await settle();
    expect(invoke).toHaveBeenCalledTimes(1);
    publish();
    await settle();
    expect(invoke).toHaveBeenCalledTimes(2);

    source.entries = [
      {
        ...descriptor,
        source: { ...descriptor.source, resource: 'extension:other:control' },
      },
    ];
    await settle();
    const before = invoke.mock.calls.length;
    publish();
    await settle();
    expect(invoke).toHaveBeenCalledTimes(before);
    publish('other');
    await settle();
    expect(invoke).toHaveBeenCalledTimes(before + 1);
    // A source without a Resource declaration is not an implicit subscriber.
    source.entries = [{ ...descriptor, source: { operation: 'read' } }];
    await settle();
    const withoutResource = invoke.mock.calls.length;
    publish();
    await settle();
    expect(invoke).toHaveBeenCalledTimes(withoutResource);
  });

  it('rejects a source action outside the declared operation allowlist', async () => {
    invoke.mockResolvedValue(stop);
    const { source } = mountSurface();
    source.entries = [{ ...descriptor, operations: ['read'] }];
    await settle();
    stopButton().click();
    await settle();
    expect(
      invoke.mock.calls.every(([, operation]) => operation === 'read'),
    ).toBe(true);
    expect(notifyError).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({
        message: 'Extension action unavailable: stop',
      }),
    );
  });

  it.each(['context', 'epoch', 'removal'])(
    'discards a pending response after %s changes',
    async (change) => {
      const previous = deferred();
      invoke.mockReturnValueOnce(previous.promise).mockResolvedValue(idle);
      const { source, props } = mountSurface();
      if (change === 'context') props.context = { session_id: 'second' };
      else if (change === 'epoch')
        source.entries = [{ ...descriptor, epoch: 'epoch-b' }];
      else source.entries = [];
      flushSync();
      previous.resolve(stop);
      await settle();
      expect(stopButton()).toBeNull();
      if (change === 'removal') expect(invoke).toHaveBeenCalledTimes(1);
      else {
        expect(document.body.textContent).toContain('Computer idle');
        expect(invoke).toHaveBeenCalledTimes(2);
        const [calledDescriptor, , params] = invoke.mock.lastCall;
        expect(calledDescriptor.epoch).toBe(
          change === 'epoch' ? 'epoch-b' : 'epoch-a',
        );
        expect(params.session_id).toBe(
          change === 'context' ? 'second' : 'first',
        );
      }
    },
  );

  it('does not restore stale source data when an action finishes during an earlier read', async () => {
    const earlier = deferred();
    const action = deferred();
    invoke
      .mockResolvedValueOnce(stop)
      .mockReturnValueOnce(earlier.promise)
      .mockReturnValueOnce(action.promise)
      .mockResolvedValueOnce(idle);
    mountSurface();
    await settle();
    invalidate();
    stopButton().click();
    flushSync();
    expect(stopButton().disabled).toBe(true);
    action.resolve({ stopped: true });
    await settle();
    expect(invoke).toHaveBeenCalledTimes(3);
    earlier.resolve({ items: [{ id: 'old', text: 'Stale control state' }] });
    await settle();
    expect(invoke).toHaveBeenCalledTimes(4);
    expect(document.body.textContent).toContain('Computer idle');
    expect(document.body.textContent).not.toContain('Stale control state');
  });
});
