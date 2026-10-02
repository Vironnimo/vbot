import { describe, expect, it } from 'vitest';

import { ariaSort, cellText, nextSort, projectRows } from '../dataTable.js';

const NAME = { id: 'name', label: 'Name' };
const CALLS = { id: 'calls', label: 'Calls', align: 'end' };

function ids(view) {
  return view.rows.map((row) => row.id);
}

describe('dataTable', () => {
  it('starts a new column in its default direction and flips the active one', () => {
    expect(nextSort(null, NAME)).toEqual({ column: 'name', direction: 'asc' });
    expect(nextSort({ column: 'name', direction: 'asc' }, CALLS)).toEqual({
      column: 'calls',
      direction: 'desc',
    });
    expect(
      nextSort(null, { ...NAME, defaultDirection: 'desc' }).direction,
    ).toBe('desc');
    expect(nextSort({ column: 'calls', direction: 'desc' }, CALLS)).toEqual({
      column: 'calls',
      direction: 'asc',
    });

    const sort = { column: 'calls', direction: 'asc' };
    expect(ariaSort(sort, CALLS)).toBe('ascending');
    expect(ariaSort({ ...sort, direction: 'desc' }, CALLS)).toBe('descending');
    expect(ariaSort(sort, NAME)).toBe('none');
    expect(ariaSort(sort, { ...NAME, sortable: false })).toBeUndefined();
  });

  it('sorts missing values last in both directions and keeps ties in incoming order', () => {
    const rows = [
      { id: 'a', calls: 2 },
      { id: 'b', calls: null },
      { id: 'c', calls: 5 },
      { id: 'd', calls: 2 },
      { id: 'e', calls: Number.NaN },
      { id: 'f' },
      { id: 'g', calls: 5 },
    ];
    const project = (direction) =>
      ids(
        projectRows({
          rows,
          columns: [CALLS],
          sort: { column: 'calls', direction },
        }),
      );

    expect(project('desc')).toEqual(['c', 'g', 'a', 'd', 'b', 'e', 'f']);
    expect(project('asc')).toEqual(['a', 'd', 'c', 'g', 'b', 'e', 'f']);
    expect(ids(projectRows({ rows, columns: [CALLS], sort: null }))).toEqual(
      ids({ rows }),
    );
  });

  it('compares text with the locale collator: numeric, case- and accent-insensitive', () => {
    const rows = [
      { id: 1, name: 'item 10' },
      { id: 2, name: 'Item 2' },
      { id: 3, name: 'beta' },
      { id: 4, name: 'Béta' },
      { id: 5, name: 'alpha' },
    ];
    const view = projectRows({
      rows,
      columns: [NAME],
      sort: { column: 'name', direction: 'asc' },
      locale: 'en',
    });

    expect(ids(view)).toEqual([5, 3, 4, 2, 1]);
  });

  it('sorts by a column sortValue instead of the row field', () => {
    const column = {
      id: 'session',
      label: 'Session',
      sortValue: (row) => row.title ?? null,
    };
    const rows = [
      { id: 'x', session: 'zzz', title: 'B' },
      { id: 'y', session: 'aaa' },
      { id: 'z', session: 'mmm', title: 'A' },
    ];

    const view = projectRows({
      rows,
      columns: [column],
      sort: { column: 'session', direction: 'asc' },
    });

    expect(ids(view)).toEqual(['z', 'x', 'y']);
  });

  it('filters by every word case-insensitively, then sorts, then limits unless expanded', () => {
    const rows = [
      { id: 'a', name: 'Claude Opus', calls: 3 },
      { id: 'b', name: 'claude haiku', calls: 9 },
      { id: 'c', name: 'GPT', calls: 7 },
      { id: 'd', name: 'Claude Sonnet', calls: 5 },
    ];
    const base = {
      rows,
      columns: [NAME, CALLS],
      sort: { column: 'calls', direction: 'desc' },
      filterText: (row) => row.name,
      limit: 2,
    };

    const limited = projectRows({ ...base, query: '  CLAUDE ' });
    expect(ids(limited)).toEqual(['b', 'd']);
    expect(limited).toMatchObject({ total: 4, matched: 3, limited: true });

    expect(
      ids(projectRows({ ...base, query: 'claude', expanded: true })),
    ).toEqual(['b', 'd', 'a']);
    expect(ids(projectRows({ ...base, query: 'opus claude' }))).toEqual(['a']);
    expect(projectRows({ ...base, query: 'opus claude' }).limited).toBe(false);
    expect(projectRows({ ...base, query: '' })).toMatchObject({
      matched: 4,
      limited: true,
    });
  });

  it('renders default cell text with locale numbers and a dash for missing values', () => {
    expect(cellText({ calls: 12345 }, CALLS, 'en')).toBe('12,345');
    expect(cellText({ calls: null }, CALLS, 'en')).toBe('—');
    expect(cellText({ name: '' }, NAME, 'en')).toBe('—');
    expect(cellText({ name: 'Opus' }, NAME, 'en')).toBe('Opus');
    expect(
      cellText({ calls: 3 }, { ...CALLS, format: (row) => `${row.calls}x` }),
    ).toBe('3x');
  });
});
