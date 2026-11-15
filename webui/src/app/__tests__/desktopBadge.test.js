// @vitest-environment jsdom

import { flushSync } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { setDesktopTaskbarBadge } = vi.hoisted(() => ({
  setDesktopTaskbarBadge: vi.fn(),
}));
vi.mock('$lib/desktopBridge.js', () => ({ setDesktopTaskbarBadge }));

import { createBadgeHarness } from './desktopBadge.support.svelte.js';

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const settle = async () => {
  flushSync();
  await new Promise((resolve) => setTimeout(resolve, 0));
};

describe('Desktop taskbar badge', () => {
  let focused;
  let harness;

  const setFocus = (value) => {
    focused = value;
    window.dispatchEvent(new Event(value ? 'focus' : 'blur'));
  };

  beforeEach(() => {
    focused = true;
    vi.spyOn(document, 'hasFocus').mockImplementation(() => focused);
    setDesktopTaskbarBadge.mockReset().mockResolvedValue({});
  });

  afterEach(() => {
    harness?.dispose();
    harness = null;
    vi.restoreAllMocks();
  });

  it('counts the displayed Session only while nobody looks at the page', async () => {
    harness = createBadgeHarness({ attended: 2, unattended: 3 });
    await settle();
    expect(setDesktopTaskbarBadge.mock.calls).toEqual([[2]]);

    setFocus(false);
    await settle();
    setFocus(true);
    await settle();
    expect(setDesktopTaskbarBadge.mock.calls).toEqual([[2], [3], [2]]);
  });

  it('ends at the latest count when counts change during a call', async () => {
    const first = deferred();
    setDesktopTaskbarBadge.mockReturnValueOnce(first.promise);
    harness = createBadgeHarness({ attended: 1 });
    await settle();

    harness.counts.attended = 4;
    await settle();
    harness.counts.attended = 5;
    await settle();
    first.resolve({});
    await settle();

    expect(setDesktopTaskbarBadge.mock.calls).toEqual([[1], [5]]);
  });

  it('sends a count again after a failed call and nothing without the capability', async () => {
    setDesktopTaskbarBadge.mockRejectedValueOnce(new Error('bridge gone'));
    harness = createBadgeHarness({ attended: 1 });
    await settle();
    harness.counts.attended = 2;
    await settle();
    harness.counts.attended = 1;
    await settle();
    expect(setDesktopTaskbarBadge.mock.calls).toEqual([[1], [2], [1]]);

    harness.counts.supported = false;
    harness.counts.attended = 7;
    await settle();
    expect(setDesktopTaskbarBadge).toHaveBeenCalledTimes(3);
  });

  it('removes the badge when the page stops counting', async () => {
    harness = createBadgeHarness({ attended: 2 });
    await settle();
    harness.dispose();
    harness = null;
    expect(setDesktopTaskbarBadge).toHaveBeenLastCalledWith(0);
  });
});
