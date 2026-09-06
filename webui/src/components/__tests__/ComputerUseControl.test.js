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
const ready = {
  available: true,
  active: true,
  stopping: false,
  hotkey_available: true,
  call_id: 'test-owned-call',
};
const button = (label) =>
  [...document.querySelectorAll('button')].find(
    (item) => item.getAttribute('aria-label') === label,
  );
async function settle() {
  flushSync();
  await vi.advanceTimersByTimeAsync(0);
  flushSync();
}

beforeEach(() => {
  vi.useFakeTimers();
  init('en');
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

it('stops immediately and ignores a stale status response', async () => {
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  let stale;
  operation.mockImplementation((_extension, _name, { action }) =>
    action === 'status'
      ? new Promise((resolve) => {
          stale = resolve;
        })
      : Promise.resolve({ ...ready, active: false }),
  );
  await vi.advanceTimersByTimeAsync(2000);
  button('Stop computer control').click();
  await settle();
  expect(operation).toHaveBeenCalledWith('computer_use', 'control', {
    action: 'stop',
    call_id: 'test-owned-call',
  });
  expect(document.querySelector('button')).toBeNull();
  stale(ready);
  await settle();
  expect(document.querySelector('button')).toBeNull();
});

it('disables Stop while the interrupted call drains and allows the next call', async () => {
  operation.mockResolvedValue({ ...ready, stopping: true });
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  expect(button('Stop computer control').disabled).toBe(true);
  operation.mockResolvedValue({ ...ready, active: false });
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(document.querySelector('button')).toBeNull();
  operation.mockResolvedValue({ ...ready, call_id: 'test-owned-next-call' });
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(button('Stop computer control').disabled).toBe(false);
  button('Stop computer control').click();
  await settle();
  expect(operation).toHaveBeenLastCalledWith('computer_use', 'control', {
    action: 'stop',
    call_id: 'test-owned-next-call',
  });
});

it('disables Stop immediately until its request settles', async () => {
  component = mount(ComputerUseControl, { target: document.body });
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

it('does not call missing extensions and discovers them on a later poll', async () => {
  list.mockResolvedValue({ extensions: [] });
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  expect(operation).not.toHaveBeenCalled();
  expect(document.querySelector('button')).toBeNull();
  list.mockResolvedValue({
    extensions: [{ name: 'computer_use', status: 'loaded' }],
  });
  await vi.advanceTimersByTimeAsync(10000);
  flushSync();
  expect(button('Stop computer control')).toBeDefined();
});

it('keeps Stop accessible if status polling fails', async () => {
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  operation.mockRejectedValue(new Error('test-owned-disconnect'));
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(button('Stop computer control').disabled).toBe(false);
});

it('adds no surface when computer control is merely available', async () => {
  operation.mockResolvedValue({ ...ready, active: false });
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  expect(document.querySelector('button')).toBeNull();
  expect(document.body.textContent.trim()).toBe('');
  operation.mockResolvedValue(ready);
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(button('Stop computer control')).toBeDefined();
  expect(document.querySelector('.banner')).toBeNull();
  operation.mockResolvedValue({ ...ready, active: false });
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(document.querySelector('button')).toBeNull();
});

it('reports a failed stop request and keeps Stop accessible', async () => {
  const onError = vi.fn();
  component = mount(ComputerUseControl, {
    target: document.body,
    props: { onError },
  });
  await settle();
  operation.mockRejectedValue(new Error('test-owned-stop-error'));
  button('Stop computer control').click();
  await settle();
  expect(onError).toHaveBeenCalledWith('test-owned-stop-error');
  expect(button('Stop computer control').disabled).toBe(false);
});

it('cleans up polling when its composer is removed', async () => {
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  await unmount(component);
  component = null;
  const count = operation.mock.calls.length;
  await vi.advanceTimersByTimeAsync(10000);
  expect(operation).toHaveBeenCalledTimes(count);
});
