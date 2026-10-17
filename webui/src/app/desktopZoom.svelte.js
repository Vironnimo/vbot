// The Desktop window zoom (capability `zoom`): the app-level owner of the
// current level, the Ctrl + Plus/Minus/0 and Ctrl + mouse wheel shortcuts, and
// the changes from Settings. WebView2's own zoom inputs are off in the
// Desktop, so every zoom change goes through here to the Desktop, which saves
// and applies it.
import { getDesktopZoom, setDesktopZoom } from '$lib/desktopBridge.js';

// Wheel movement (CSS pixels) that zooms one step: a mouse wheel notch, or a
// stretch of a touchpad pinch, which arrives as many small Ctrl + wheel events.
const WHEEL_STEP_DELTA = 40;
// One mouse wheel notch. Chromium merges quick notches into one event, which
// then zooms one step per notch.
const WHEEL_NOTCH_PIXELS = 100;
const WHEEL_LINE_PIXELS = 40;
const WHEEL_PAGE_PIXELS = 800;

/**
 * The zoom action of a keydown, like a browser's: `in` for Ctrl + Plus (also
 * Ctrl + =), `out` for Ctrl + Minus, `reset` for Ctrl + 0, else null. It reads
 * the character, so it follows the keyboard layout. Alt excludes it, so AltGr
 * characters never zoom.
 */
export function zoomShortcut(event) {
  if (!(event.ctrlKey || event.metaKey) || event.altKey) return null;
  if (event.key === '+' || event.key === '=') return 'in';
  if (event.key === '-') return 'out';
  if (event.key === '0') return 'reset';
  return null;
}

/** The level one step `in` or `out` from `percent`, staying at the ends. */
export function nextZoomLevel(levels, percent, direction) {
  if (direction === 'in') {
    return levels.find((level) => level > percent) ?? levels.at(-1);
  }
  return levels.findLast((level) => level < percent) ?? levels[0];
}

function wheelPixels(event) {
  if (event.deltaMode === 1) return event.deltaY * WHEEL_LINE_PIXELS;
  if (event.deltaMode === 2) return event.deltaY * WHEEL_PAGE_PIXELS;
  return event.deltaY;
}

/**
 * Create the zoom owner. `onError(error)` hears a failed change; the owner then
 * reads the Desktop's level again. `zoom` is null until the first read.
 */
export function createDesktopZoom({ onError = () => {} } = {}) {
  let zoom = $state(null);
  // The latest level still to send; changes made while one is under way
  // collapse into it, so the Desktop ends at the last level asked for.
  let wanted = null;
  let sending = false;
  let wheelDelta = 0;
  let target = null;

  async function load() {
    try {
      zoom = await getDesktopZoom();
    } catch {
      // Keep the current level; the next change or start reads it again.
    }
  }

  async function flush() {
    if (sending) return;
    sending = true;
    try {
      while (wanted !== null) {
        const percent = wanted;
        wanted = null;
        try {
          const status = await setDesktopZoom(percent);
          if (wanted === null) zoom = status;
        } catch (error) {
          wanted = null;
          onError(error);
          await load();
        }
      }
    } finally {
      sending = false;
    }
  }

  function set(percent) {
    if (!zoom || !zoom.levels.includes(percent)) return;
    zoom = { ...zoom, percent };
    wanted = percent;
    void flush();
  }

  function apply(action) {
    if (!zoom) return;
    set(
      action === 'reset'
        ? zoom.default
        : nextZoomLevel(zoom.levels, zoom.percent, action),
    );
  }

  // Bubble phase: a control that handles the keys itself (a shortcut
  // capture) prevents the default first.
  function handleKeydown(event) {
    if (event.defaultPrevented) return;
    const action = zoomShortcut(event);
    if (!action) return;
    event.preventDefault();
    apply(action);
  }

  function handleWheel(event) {
    if (!(event.ctrlKey || event.metaKey)) {
      wheelDelta = 0;
      return;
    }
    event.preventDefault();
    const pixels = wheelPixels(event);
    if (Math.sign(pixels) !== Math.sign(wheelDelta)) wheelDelta = 0;
    wheelDelta += pixels;
    const distance = Math.abs(wheelDelta);
    if (distance < WHEEL_STEP_DELTA) return;
    const steps = Math.max(1, Math.round(distance / WHEEL_NOTCH_PIXELS));
    for (let step = 0; step < steps; step += 1) {
      apply(wheelDelta < 0 ? 'in' : 'out');
    }
    wheelDelta = 0;
  }

  function start(eventTarget = window) {
    if (target) return;
    target = eventTarget;
    target.addEventListener('keydown', handleKeydown);
    // Capture and non-passive: no scroll container sees a zooming wheel.
    target.addEventListener('wheel', handleWheel, {
      capture: true,
      passive: false,
    });
    void load();
  }

  function stop() {
    if (!target) return;
    target.removeEventListener('keydown', handleKeydown);
    target.removeEventListener('wheel', handleWheel, { capture: true });
    target = null;
  }

  return {
    get zoom() {
      return zoom;
    },
    set,
    start,
    stop,
  };
}
