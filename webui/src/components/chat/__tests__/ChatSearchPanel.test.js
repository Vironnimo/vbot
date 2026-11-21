// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../../lib/i18n.js';
vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
const { default: Panel } = await import('../ChatSearchPanel.svelte');

it('focuses the query, searches on Enter and steps through hits without consuming IME confirmation', async () => {
  init('en');
  const hit = { sequence: 3, snippet: 'match', ranges: [[0, 5]], role: 'user' };
  const search = {
    hits: [hit],
    selected: null,
    query: 'match',
    complete: true,
    refresh: vi.fn(),
  };
  const onChoose = vi.fn().mockResolvedValue(true);
  const panel = mount(Panel, {
    target: document.body,
    props: {
      search,
      onChoose,
      onClose: vi.fn(),
      agentName: 'Agent',
      narrow: false,
    },
  });
  try {
    flushSync();
    const field = document.querySelector('input');
    expect(document.activeElement).toBe(field);
    const press = (key, isComposing = false) =>
      field.dispatchEvent(
        new KeyboardEvent('keydown', {
          key,
          isComposing,
          bubbles: true,
          cancelable: true,
        }),
      );
    press('Enter', true);
    expect(search.refresh).not.toHaveBeenCalled();
    press('Enter');
    expect(search.refresh).toHaveBeenCalledWith(true);
    press('ArrowDown');
    press('ArrowUp');
    expect(onChoose.mock.calls).toEqual([[hit], [hit]]);
    expect(document.querySelector('.result mark').textContent).toBe('match');
  } finally {
    await unmount(panel);
  }
});
