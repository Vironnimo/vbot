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
  paused: false,
  active: false,
  controlling: true,
  hotkey_available: true,
  stop_token: 'test-owned-stop',
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
      : Promise.resolve({ ...ready, paused: true }),
  );
  await vi.advanceTimersByTimeAsync(2000);
  button('Stop computer control').click();
  await settle();
  expect(operation).toHaveBeenCalledWith('computer_use', 'control', {
    action: 'stop',
  });
  expect(button('Allow computer control')).toBeDefined();
  stale(ready);
  await settle();
  expect(button('Allow computer control')).toBeDefined();
});

it('does not allow resume until interrupted work has drained', async () => {
  operation.mockResolvedValue({ ...ready, paused: true, active: true });
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  expect(button('Allow computer control').disabled).toBe(true);
  operation.mockResolvedValue({ ...ready, paused: true });
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  button('Allow computer control').click();
  await settle();
  expect(operation).toHaveBeenCalledWith('computer_use', 'control', {
    action: 'resume',
    stop_token: 'test-owned-stop',
  });
});

it('keeps Stop available during resume and ignores a late resume response', async () => {
  operation.mockResolvedValue({ ...ready, paused: true });
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  let resume;
  operation.mockImplementation((_extension, _name, { action }) =>
    action === 'resume'
      ? new Promise((resolve) => {
          resume = resolve;
        })
      : Promise.resolve({ ...ready, paused: true }),
  );
  button('Allow computer control').click();
  await settle();
  expect(button('Stop computer control').disabled).toBe(false);
  button('Stop computer control').click();
  await settle();
  resume(ready);
  await settle();
  expect(button('Allow computer control')).toBeDefined();
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
  operation.mockResolvedValue({ ...ready, controlling: false });
  component = mount(ComputerUseControl, { target: document.body });
  await settle();
  expect(document.querySelector('button')).toBeNull();
  expect(document.body.textContent.trim()).toBe('');
  operation.mockResolvedValue(ready);
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(button('Stop computer control')).toBeDefined();
  expect(document.querySelector('.banner')).toBeNull();
  operation.mockResolvedValue({ ...ready, controlling: false });
  await vi.advanceTimersByTimeAsync(2000);
  flushSync();
  expect(document.querySelector('button')).toBeNull();
});

it('shows only the recovery control after stop and reports mutation failures', async () => {
  operation.mockResolvedValue({ ...ready, controlling: false, paused: true });
  const onError = vi.fn();
  component = mount(ComputerUseControl, {
    target: document.body,
    props: { onError },
  });
  await settle();
  expect(button('Allow computer control')).toBeDefined();
  expect(button('Stop computer control')).toBeUndefined();
  operation.mockRejectedValue(new Error('test-owned-resume-error'));
  button('Allow computer control').click();
  await settle();
  expect(onError).toHaveBeenCalledWith('test-owned-resume-error');
  expect(button('Allow computer control').disabled).toBe(false);
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
