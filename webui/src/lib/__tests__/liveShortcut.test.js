import { beforeEach, describe, expect, it } from 'vitest';

import { init } from '../i18n.js';
import {
  formatLiveShortcut,
  isModifierKeyCode,
  liveShortcutFromKeyboardEvent,
  loadKeyboardLayoutMap,
} from '../liveShortcut.js';

beforeEach(() => init('en'));

describe('formatLiveShortcut', () => {
  it('lists modifiers in a fixed order before the key', () => {
    expect(
      formatLiveShortcut({
        win: true,
        shift: true,
        alt: true,
        ctrl: true,
        key: 'Space',
      }),
    ).toBe('Ctrl + Alt + Shift + Win + Space');
    expect(formatLiveShortcut({ key: 'F13' })).toBe('F13');
    expect(formatLiveShortcut({ ctrl: true, key: 'Digit7' })).toBe('Ctrl + 7');
  });

  it('labels letters as the keyboard layout prints them', () => {
    const germanLayout = new Map([['KeyY', 'z']]);

    expect(formatLiveShortcut({ alt: true, key: 'KeyY' }, germanLayout)).toBe(
      'Alt + Z',
    );
    expect(formatLiveShortcut({ alt: true, key: 'KeyY' })).toBe('Alt + Y');
  });

  it('is empty without a key', () => {
    expect(formatLiveShortcut(null)).toBe('');
    expect(formatLiveShortcut({ ctrl: true, key: '' })).toBe('');
  });
});

describe('keyboard capture', () => {
  it('describes a key press in the Desktop shape', () => {
    expect(
      liveShortcutFromKeyboardEvent({
        ctrlKey: true,
        altKey: false,
        shiftKey: true,
        metaKey: true,
        code: 'KeyL',
      }),
    ).toEqual({ ctrl: true, alt: false, shift: true, win: true, key: 'KeyL' });
  });

  it('recognizes modifier keys', () => {
    expect(isModifierKeyCode('AltRight')).toBe(true);
    expect(isModifierKeyCode('MetaLeft')).toBe(true);
    expect(isModifierKeyCode('Space')).toBe(false);
  });

  it('falls back when the layout map is unavailable', async () => {
    expect(await loadKeyboardLayoutMap(undefined)).toBeNull();
    expect(
      await loadKeyboardLayoutMap({
        getLayoutMap: () => Promise.reject(new Error('insecure')),
      }),
    ).toBeNull();
    const layout = new Map();
    expect(
      await loadKeyboardLayoutMap({ getLayoutMap: async () => layout }),
    ).toBe(layout);
  });
});
