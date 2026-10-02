// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';

const list = vi.fn();
const operation = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  listExtensions: (...args) => list(...args),
  extensionOperation: (...args) => operation(...args),
}));
const { default: ComputerUseControl } =
  await import('../ComputerUseControl.svelte');
let component;
let listeners;
const ready = {
  available: true,
  active: true,
  stopping: false,
  hotkey_available: true,
  control_id: 'test-owned-control',
};
// App's Extension invalidations: a change Computer Use published, another
// Extension's change, and an invalidation without owner (reconnect, reload).
const changed = { owner: 'computer_use', change: { resource: 'control' } };
const unrelated = { owner: 'swarm', change: { resource: 'swarms' } };
const everything = { owner: null, change: null };
const button = (label) =>
  [...document.querySelectorAll('button')].find(
    (item) => item.getAttribute('aria-label') === label,
  );
async function settle() {
  flushSync();
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
}
function subscribeInvalidations(listener) {
  listeners.push(listener);
  return () => listeners.splice(listeners.indexOf(listener), 1);
}
async function invalidate(invalidation) {
  for (const listener of [...listeners]) listener(invalidation);
  await settle();
}
function mountControl(props = {}) {
  component = mount(ComputerUseControl, {
    target: document.body,
    props: { subscribeInvalidations, ...props },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  init('en');
  listeners = [];
  list.mockResolvedValue({
    extensions: [{ name: 'computer_use', status: 'loaded' }],
  });
  operation.mockResolvedValue(ready);
});

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.resetAllMocks();
  vi.useRealTimers();
});

it('reads its status only on Computer Use changes and owner-less invalidations', async () => {
  operation.mockResolvedValue({ ...ready, active: false });
  mountControl();
  await settle();
  expect(list).toHaveBeenCalledOnce();
  expect(operation).toHaveBeenCalledOnce();

  await vi.advanceTimersByTimeAsync(60_000);
  await invalidate(unrelated);
  expect(list).toHaveBeenCalledOnce();
  expect(operation).toHaveBeenCalledOnce();

  operation.mockResolvedValue(ready);
  await invalidate(changed);
  expect(list).toHaveBeenCalledOnce();
  expect(operation).toHaveBeenCalledTimes(2);
  expect(button('Stop computer control')).toBeDefined();

  await invalidate(everything);
  expect(list).toHaveBeenCalledTimes(2);
  expect(operation).toHaveBeenCalledTimes(3);
});

it('reads once more after a change that arrives during a read', async () => {
  let first;
  operation.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        first = resolve;
      }),
  );
  mountControl();
  await settle();
  await invalidate(changed);
  await invalidate(changed);
  expect(operation).toHaveBeenCalledOnce();
  operation.mockResolvedValue({ ...ready, active: false });
  first(ready);
  await settle();
  expect(operation).toHaveBeenCalledTimes(2);
  expect(document.querySelector('button')).toBeNull();
});

it('stops immediately and ignores a stale status response', async () => {
  mountControl();
  await settle();
  let stale;
  operation.mockImplementation((_extension, _name, { action }) =>
    action === 'status'
      ? new Promise((resolve) => {
          stale = resolve;
        })
      : Promise.resolve({ ...ready, active: false }),
  );
  await invalidate(changed);
  button('Stop computer control').click();
  await settle();
  expect(operation).toHaveBeenCalledWith('computer_use', 'control', {
    action: 'stop',
    control_id: 'test-owned-control',
  });
  expect(document.querySelector('button')).toBeNull();
  stale(ready);
  await settle();
  expect(document.querySelector('button')).toBeNull();
});

it('reads the status published while its stop request was in flight', async () => {
  mountControl();
  await settle();
  let stop;
  operation.mockImplementation((_extension, _name, { action }) =>
    action === 'stop'
      ? new Promise((resolve) => {
          stop = resolve;
        })
      : Promise.resolve({ ...ready, active: false }),
  );
  button('Stop computer control').click();
  await settle();
  // Control ended before the stop response arrived.
  await invalidate(changed);
  expect(operation).toHaveBeenCalledTimes(2);
  stop({ ...ready, stopping: true });
  await settle();
  expect(operation).toHaveBeenCalledTimes(3);
  expect(document.querySelector('button')).toBeNull();
});

it('disables Stop while the interrupted call drains and allows the next control', async () => {
  operation.mockResolvedValue({ ...ready, stopping: true });
  mountControl();
  await settle();
  expect(button('Stop computer control').disabled).toBe(true);
  operation.mockResolvedValue({ ...ready, active: false });
  await invalidate(changed);
  expect(document.querySelector('button')).toBeNull();
  operation.mockResolvedValue({
    ...ready,
    control_id: 'test-owned-next-control',
  });
  await invalidate(changed);
  expect(button('Stop computer control').disabled).toBe(false);
  button('Stop computer control').click();
  await settle();
  expect(operation).toHaveBeenLastCalledWith('computer_use', 'control', {
    action: 'stop',
    control_id: 'test-owned-next-control',
  });
});

it('disables Stop immediately until its request settles', async () => {
  mountControl();
  await settle();
  let stop;
  operation.mockImplementation((_extension, _name, { action }) =>
    action === 'stop'
      ? new Promise((resolve) => {
          stop = resolve;
        })
      : Promise.resolve(ready),
  );
  button('Stop computer control').click();
  await settle();
  expect(button('Stop computer control').disabled).toBe(true);
  stop({ ...ready, active: false });
  await settle();
  expect(document.querySelector('button')).toBeNull();
});

it('does not call missing extensions and discovers them on an owner-less invalidation', async () => {
  list.mockResolvedValue({ extensions: [] });
  mountControl();
  await settle();
  expect(operation).not.toHaveBeenCalled();
  expect(document.querySelector('button')).toBeNull();
  list.mockResolvedValue({
    extensions: [{ name: 'computer_use', status: 'loaded' }],
  });
  await invalidate(everything);
  expect(button('Stop computer control')).toBeDefined();

  // Disabling Computer Use ends its control, so the control disappears.
  list.mockResolvedValue({ extensions: [] });
  await invalidate(everything);
  expect(document.querySelector('button')).toBeNull();
});

it('adds no surface when computer control is merely available', async () => {
  operation.mockResolvedValue({ ...ready, active: false });
  mountControl();
  await settle();
  expect(document.querySelector('button')).toBeNull();
  expect(document.body.textContent.trim()).toBe('');
  operation.mockResolvedValue(ready);
  await invalidate(changed);
  expect(button('Stop computer control')).toBeDefined();
  expect(document.querySelector('.banner')).toBeNull();
  operation.mockResolvedValue({ ...ready, active: false });
  await invalidate(changed);
  expect(document.querySelector('button')).toBeNull();
});

it('keeps Stop accessible when a status read or the stop request fails', async () => {
  const onError = vi.fn();
  mountControl({ onError });
  await settle();
  operation.mockRejectedValue(new Error('test-owned-disconnect'));
  await invalidate(changed);
  expect(button('Stop computer control').disabled).toBe(false);
  expect(onError).not.toHaveBeenCalled();

  operation.mockRejectedValue(new Error('test-owned-stop-error'));
  button('Stop computer control').click();
  await settle();
  expect(onError).toHaveBeenCalledExactlyOnceWith('test-owned-stop-error');
  expect(button('Stop computer control').disabled).toBe(false);
});

it('unsubscribes when its composer is removed', async () => {
  mountControl();
  await settle();
  await unmount(component);
  component = null;
  expect(listeners).toEqual([]);
  const count = operation.mock.calls.length;
  await vi.advanceTimersByTimeAsync(10000);
  expect(operation).toHaveBeenCalledTimes(count);
});
