// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '$lib/i18n.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
const { default: StatePreview } = await import('../StatePreview.svelte');
let component;
afterEach(async () => {
  if (component) await unmount(component);
  document.body.innerHTML = '';
});
async function settle() {
  for (let n = 0; n < 10; n++) {
    await Promise.resolve();
    flushSync();
  }
}

it.each([
  'Beginning of a large document. '.repeat(10000) + ' END_MARKER',
  {
    messages: Array.from({ length: 10000 }, (_, n) => ({
      n,
      body: 'A message',
    })),
    final: 'END_MARKER',
  },
])(
  'keeps large state previews bounded and reveals the complete snapshot only on request',
  async (state) => {
    init('en');
    component = mount(StatePreview, {
      target: document.body,
      props: { state },
    });
    await settle();
    const text =
      typeof state === 'string' ? state : JSON.stringify(state, null, 2);
    expect(
      document.querySelector('.jev-state-excerpt').textContent.length,
    ).toBeLessThanOrEqual(401);
    expect(document.body.textContent).not.toContain('END_MARKER');
    expect(document.querySelector('textarea')).toBeNull();
    const opener = document.querySelector('button');
    opener.focus();
    opener.click();
    await settle();
    const reader = document.querySelector('[role="dialog"] textarea');
    expect(reader.value).toBe(text);
    expect(reader.readOnly).toBe(true);
    document.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
    );
    await settle();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.activeElement).toBe(opener);
  },
);
