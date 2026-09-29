// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRawSnippet, flushSync, mount, tick, unmount } from 'svelte';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: SortableList } = await import('../SortableList.svelte');
const { isDragExempt, orderByKeys } = await import('../sortable.js');
const { t } = await import('../../../lib/i18n.js');

const ITEMS = [
  { id: 'alpha', name: 'Alpha' },
  { id: 'bravo', name: 'Bravo' },
  { id: 'charlie', name: 'Charlie' },
];

const row = createRawSnippet((entry) => ({
  render: () =>
    `<span><button type="button" data-row="${entry().id}">${entry().name}</button><input data-input="${entry().id}"></span>`,
}));

let component;

afterEach(() => {
  if (component) unmount(component);
  component = null;
  document.body.innerHTML = '';
});

function render(props) {
  component = mount(SortableList, {
    target: document.body,
    props: {
      items: ITEMS,
      getLabel: (entry) => entry.name,
      item: row,
      ...props,
    },
  });
  flushSync();
}

function renderedOrder() {
  return [...document.querySelectorAll('[data-sortable-key]')].map(
    (element) => element.dataset.sortableKey,
  );
}

function press(target, key, options = {}) {
  target.focus();
  target.dispatchEvent(
    new KeyboardEvent('keydown', {
      key,
      altKey: true,
      bubbles: true,
      cancelable: true,
      ...options,
    }),
  );
}

describe('SortableList', () => {
  it('moves the focused row with Alt+Arrow keys, announces it and keeps focus', async () => {
    const onReorder = vi.fn();
    render({ onReorder });

    press(document.querySelector('[data-row="bravo"]'), 'ArrowUp');
    await tick();
    await tick();

    expect(onReorder).toHaveBeenCalledWith(1, 0);
    expect(document.activeElement.dataset.row).toBe('bravo');
    expect(document.querySelector('[role="status"]').textContent.trim()).toBe(
      t('common.sortable.moved', { name: 'Bravo', position: 1, total: 3 }),
    );
  });

  it.each([
    ['a text field', '[data-input="bravo"]', {}],
    ['a key without Alt', '[data-row="bravo"]', { altKey: false }],
    ['the list boundary', '[data-row="alpha"]', {}],
  ])('ignores a move request from %s', async (_case, selector, options) => {
    const onReorder = vi.fn();
    render({ onReorder });

    press(document.querySelector(selector), 'ArrowUp', options);
    await tick();

    expect(onReorder).not.toHaveBeenCalled();
  });

  it('shows the moved order until the owner settles the save, then its items', async () => {
    let settle;
    const onReorder = vi.fn(
      () =>
        new Promise((resolve) => {
          settle = resolve;
        }),
    );
    render({ onReorder });

    press(document.querySelector('[data-row="alpha"]'), 'ArrowDown');
    await tick();
    expect(renderedOrder()).toEqual(['bravo', 'alpha', 'charlie']);

    // A second move is refused while the first one is saved.
    press(document.querySelector('[data-row="charlie"]'), 'ArrowUp');
    expect(onReorder).toHaveBeenCalledTimes(1);

    // The owner rejected the move and kept its order: the list follows it.
    settle();
    await tick();
    await tick();
    expect(renderedOrder()).toEqual(['alpha', 'bravo', 'charlie']);
  });

  it('refuses moves while disabled', async () => {
    const onReorder = vi.fn();
    render({ onReorder, disabled: true });

    press(document.querySelector('[data-row="bravo"]'), 'ArrowUp');
    await tick();

    expect(onReorder).not.toHaveBeenCalled();
  });
});

describe('sortable policy', () => {
  it('starts drags from rows and buttons but never from text entry', () => {
    document.body.innerHTML = `
      <li><span id="text">Row</span><button id="button">x</button>
      <input id="input"><input id="check" type="checkbox"><textarea id="area"></textarea>
      <div id="ignored" data-sortable-ignore><span id="inner">x</span></div></li>`;
    const exempt = (id) => isDragExempt(document.getElementById(id));

    expect(exempt('text')).toBe(false);
    expect(exempt('button')).toBe(false);
    expect(exempt('check')).toBe(false);
    expect(exempt('input')).toBe(true);
    expect(exempt('area')).toBe(true);
    expect(exempt('inner')).toBe(true);
  });

  it('orders items by draft keys, keeping unknown items and dropping missing keys', () => {
    const items = [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }];

    expect(
      orderByKeys(items, ['c', 'x', 'a', 'b'], (item) => item.id).map(
        (item) => item.id,
      ),
    ).toEqual(['c', 'a', 'b', 'd']);
  });
});
