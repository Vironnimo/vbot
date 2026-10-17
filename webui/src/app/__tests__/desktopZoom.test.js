// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { getDesktopZoom, setDesktopZoom } = vi.hoisted(() => ({
  getDesktopZoom: vi.fn(),
  setDesktopZoom: vi.fn(),
}));
vi.mock('$lib/desktopBridge.js', () => ({ getDesktopZoom, setDesktopZoom }));

import { createDesktopZoom, zoomShortcut } from '../desktopZoom.svelte.js';

const LEVELS = [50, 67, 75, 80, 90, 100, 110, 125, 150, 175, 200];

function status(percent) {
  return { supported: true, percent, levels: LEVELS, default: 100 };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function key(init) {
  return new KeyboardEvent('keydown', { cancelable: true, ...init });
}

function wheel(init) {
  return new WheelEvent('wheel', { cancelable: true, ...init });
}

describe('zoomShortcut', () => {
  it.each([
    [{ ctrlKey: true, key: '+' }, 'in'],
    [{ ctrlKey: true, shiftKey: true, key: '+' }, 'in'],
    [{ ctrlKey: true, key: '=' }, 'in'],
    [{ ctrlKey: true, key: '-' }, 'out'],
    [{ ctrlKey: true, key: '0' }, 'reset'],
    [{ key: '+' }, null],
    [{ ctrlKey: true, altKey: true, key: '0' }, null],
    [{ ctrlKey: true, key: 'a' }, null],
  ])('reads %o as %s', (init, action) => {
    expect(zoomShortcut(key(init))).toBe(action);
  });
});

describe('Desktop zoom', () => {
  let zoom;
  let onError;

  beforeEach(async () => {
    getDesktopZoom.mockReset().mockResolvedValue(status(100));
    setDesktopZoom
      .mockReset()
      .mockImplementation(async (percent) => status(percent));
    onError = vi.fn();
    zoom = createDesktopZoom({ onError });
    zoom.start(window);
    await vi.waitFor(() => expect(zoom.zoom?.percent).toBe(100));
  });

  afterEach(() => {
    zoom.stop();
  });

  it('steps through the levels with the keys and resets to the default', async () => {
    const plus = key({ ctrlKey: true, key: '+' });
    window.dispatchEvent(plus);
    expect(plus.defaultPrevented).toBe(true);
    expect(zoom.zoom.percent).toBe(110);
    await vi.waitFor(() =>
      expect(setDesktopZoom).toHaveBeenLastCalledWith(110),
    );

    window.dispatchEvent(key({ ctrlKey: true, key: '0' }));
    window.dispatchEvent(key({ ctrlKey: true, key: '-' }));
    expect(zoom.zoom.percent).toBe(90);
    await vi.waitFor(() => expect(setDesktopZoom).toHaveBeenLastCalledWith(90));

    // A control that handles the keys itself keeps them.
    const captured = key({ ctrlKey: true, key: '-' });
    captured.preventDefault();
    window.dispatchEvent(captured);
    expect(zoom.zoom.percent).toBe(90);
  });

  it('zooms one step per wheel notch or stretch of a touchpad pinch', () => {
    const notch = wheel({ ctrlKey: true, deltaY: -100 });
    window.dispatchEvent(notch);
    expect(notch.defaultPrevented).toBe(true);
    expect(zoom.zoom.percent).toBe(110);

    for (let index = 0; index < 3; index += 1) {
      window.dispatchEvent(wheel({ ctrlKey: true, deltaY: 15 }));
    }
    expect(zoom.zoom.percent).toBe(100);

    // Quick notches arrive merged into one event.
    window.dispatchEvent(wheel({ ctrlKey: true, deltaY: -200 }));
    expect(zoom.zoom.percent).toBe(125);

    const scroll = wheel({ deltaY: 100 });
    window.dispatchEvent(scroll);
    expect(scroll.defaultPrevented).toBe(false);
    expect(zoom.zoom.percent).toBe(125);
  });

  it('stays at the ends of the levels', () => {
    zoom.set(200);
    window.dispatchEvent(key({ ctrlKey: true, key: '+' }));
    expect(zoom.zoom.percent).toBe(200);
  });

  it('sends only the latest level while a change is under way', async () => {
    const first = deferred();
    setDesktopZoom.mockReturnValueOnce(first.promise);
    zoom.set(110);
    zoom.set(125);
    zoom.set(150);
    first.resolve(status(110));

    await vi.waitFor(() => expect(setDesktopZoom).toHaveBeenCalledTimes(2));
    expect(setDesktopZoom.mock.calls).toEqual([[110], [150]]);
    expect(zoom.zoom.percent).toBe(150);
  });

  it('reports a failed change and shows the Desktop level again', async () => {
    setDesktopZoom.mockRejectedValueOnce(new Error('disk full'));
    zoom.set(125);

    await vi.waitFor(() => expect(onError).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(zoom.zoom.percent).toBe(100));
  });
});
