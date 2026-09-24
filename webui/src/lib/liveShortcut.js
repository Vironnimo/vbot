/** Pure helpers for the Desktop's global Live voice shortcut. */

import { t } from './i18n.js';

// Modifier keys alone never complete a shortcut; capture waits for the key.
const MODIFIER_KEY_CODES = new Set([
  'ShiftLeft',
  'ShiftRight',
  'ControlLeft',
  'ControlRight',
  'AltLeft',
  'AltRight',
  'MetaLeft',
  'MetaRight',
  'OSLeft',
  'OSRight',
]);
const LETTER_CODE = /^Key([A-Z])$/;
const DIGIT_CODE = /^Digit([0-9])$/;

/** True when `code` (a `KeyboardEvent.code`) is a modifier key. */
export function isModifierKeyCode(code) {
  return MODIFIER_KEY_CODES.has(code);
}

/**
 * The shortcut a key press describes, in the Desktop's shape: modifier flags
 * plus the physical key as `KeyboardEvent.code`. The Desktop validates it.
 */
export function liveShortcutFromKeyboardEvent(event) {
  return {
    ctrl: Boolean(event.ctrlKey),
    alt: Boolean(event.altKey),
    shift: Boolean(event.shiftKey),
    win: Boolean(event.metaKey),
    key: typeof event.code === 'string' ? event.code : '',
  };
}

// Letters follow the keyboard layout (the Desktop registers the physical key,
// so a German keyboard's `KeyY` is labelled Z); digits, F-keys and Space keep
// their names.
function keyLabel(code, layoutMap) {
  const letter = LETTER_CODE.exec(code);
  if (letter) {
    const mapped = layoutMap?.get?.(code);
    return (
      typeof mapped === 'string' && mapped.length === 1 ? mapped : letter[1]
    ).toUpperCase();
  }
  const digit = DIGIT_CODE.exec(code);
  if (digit) return digit[1];
  if (code === 'Space') return t('settings.liveShortcut.space', 'Space');
  return code;
}

/** Display text such as "Ctrl + Alt + Space"; empty without a key. */
export function formatLiveShortcut(shortcut, layoutMap = null) {
  if (typeof shortcut?.key !== 'string' || !shortcut.key) return '';
  const parts = [];
  if (shortcut.ctrl === true) parts.push('Ctrl');
  if (shortcut.alt === true) parts.push('Alt');
  if (shortcut.shift === true) parts.push('Shift');
  if (shortcut.win === true) parts.push('Win');
  parts.push(keyLabel(shortcut.key, layoutMap));
  return parts.join(' + ');
}

/** The browser's keyboard layout map, or null where it is unavailable. */
export async function loadKeyboardLayoutMap(
  keyboard = globalThis.navigator?.keyboard,
) {
  try {
    return (await keyboard?.getLayoutMap?.()) ?? null;
  } catch {
    return null;
  }
}
