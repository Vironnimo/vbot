// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: DataTable } = await import('../DataTable.svelte');

const COLUMNS = [
  { id: 'name', label: 'Model' },
  { id: 'calls', label: 'Calls', align: 'end', hint: 'Model requests.' },
  { id: 'note', label: 'Note', sortable: false },
];

const ROWS = [
  { id: 'opus', name: 'Opus', calls: 3, note: 'a' },
  { id: 'haiku', name: 'Haiku', calls: 9, note: 'b' },
  { id: 'gpt', name: 'GPT', calls: null, note: 'c' },
  { id: 'sonnet', name: 'Sonnet', calls: 5, note: 'd' },
];

describe('DataTable', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
  });

  function render(props = {}) {
    mountedComponent = mount(DataTable, {
      target: document.body,
      props: {
        columns: COLUMNS,
        rows: ROWS,
        rowKey: (row) => row.id,
        ariaLabel: 'Models',
        ...props,
      },
    });
    flushSync();
  }

  function headers() {
    return [...document.querySelectorAll('thead th')];
  }

  function firstColumn() {
    return [...document.querySelectorAll('tbody tr')].map((row) =>
      row.cells[0].textContent.trim(),
    );
  }

  function click(element) {
    element.click();
    flushSync();
  }

  it('sorts from header buttons and reports the order through aria-sort', () => {
    render({ initialSort: { column: 'calls', direction: 'desc' } });
    const [name, calls, note] = headers();

    expect(document.querySelector('table').getAttribute('aria-label')).toBe(
      'Models',
    );
    expect(calls.getAttribute('aria-sort')).toBe('descending');
    expect(name.getAttribute('aria-sort')).toBe('none');
    expect(note.hasAttribute('aria-sort')).toBe(false);
    expect(note.querySelector('button')).toBeNull();
    expect(firstColumn()).toEqual(['Haiku', 'Sonnet', 'Opus', 'GPT']);
    expect(calls.querySelector('.info-hint').getAttribute('aria-label')).toBe(
      'About Calls',
    );

    click(calls.querySelector('.data-table__sort'));
    expect(calls.getAttribute('aria-sort')).toBe('ascending');
    expect(firstColumn()).toEqual(['Opus', 'Sonnet', 'Haiku', 'GPT']);

    click(name.querySelector('.data-table__sort'));
    expect(name.getAttribute('aria-sort')).toBe('ascending');
    expect(calls.getAttribute('aria-sort')).toBe('none');
    expect(firstColumn()).toEqual(['GPT', 'Haiku', 'Opus', 'Sonnet']);
  });

  it('limits rows behind a Show all / Show fewer toggle', () => {
    render({ limit: 2, initialSort: { column: 'name', direction: 'asc' } });
    const toggle = () => document.querySelector('.data-table__more');

    expect(firstColumn()).toEqual(['GPT', 'Haiku']);
    expect(toggle().textContent.trim()).toBe('Show all 4');
    expect(toggle().getAttribute('aria-expanded')).toBe('false');
    expect(toggle().getAttribute('aria-controls')).toBe(
      document.querySelector('table').id,
    );

    click(toggle());
    expect(firstColumn()).toEqual(['GPT', 'Haiku', 'Opus', 'Sonnet']);
    expect(toggle().textContent.trim()).toBe('Show fewer');

    click(toggle());
    expect(firstColumn()).toEqual(['GPT', 'Haiku']);
  });

  it('filters rows by their text and counts the matches', () => {
    render({
      filter: { placeholder: 'Filter Models', text: (row) => row.name },
    });
    const input = document.querySelector('input[type="search"]');
    const count = document.querySelector('.data-table__count');

    expect(input.getAttribute('aria-label')).toBe('Filter Models');
    expect(count.textContent.trim()).toBe('');

    input.value = 'O';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(firstColumn()).toEqual(['Opus', 'Sonnet']);
    expect(count.textContent.trim()).toBe('2 of 4');

    input.value = 'zzz';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    expect(document.querySelector('table')).toBeNull();
    expect(document.querySelector('.empty-state')).toBeTruthy();
  });

  it('activates rows by click, Enter and Space, but not from controls inside them', () => {
    const onRowClick = vi.fn();
    render({ onRowClick });
    const row = document.querySelector('tbody tr');

    expect(row.tabIndex).toBe(0);
    click(row.cells[0]);
    expect(onRowClick).toHaveBeenLastCalledWith(ROWS[0]);

    for (const key of ['Enter', ' ']) {
      const event = new KeyboardEvent('keydown', {
        key,
        bubbles: true,
        cancelable: true,
      });
      row.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(true);
    }
    expect(onRowClick).toHaveBeenCalledTimes(3);

    const control = document.createElement('button');
    row.cells[2].appendChild(control);
    click(control);
    expect(onRowClick).toHaveBeenCalledTimes(3);
  });

  it('shows the empty text when there are no rows', () => {
    render({ rows: [], emptyText: 'No Model calls yet.' });

    expect(document.querySelector('table')).toBeNull();
    expect(document.querySelector('.empty-state').textContent).toContain(
      'No Model calls yet.',
    );
  });
});
