import { describe, expect, it } from 'vitest';

import { countAllowed, filterChipsByQuery } from '../accessChips.js';

describe('filterChipsByQuery', () => {
  const items = [
    { name: 'read' },
    { name: 'web_search' },
    { name: 'web_fetch' },
    { name: 'grep' },
  ];

  it('returns the full list for a blank query', () => {
    expect(filterChipsByQuery(items, '')).toBe(items);
    expect(filterChipsByQuery(items, '   ')).toBe(items);
  });

  it('matches a case-insensitive substring of the name', () => {
    expect(filterChipsByQuery(items, 'WEB').map((i) => i.name)).toEqual([
      'web_search',
      'web_fetch',
    ]);
  });

  it('returns an empty list when nothing matches', () => {
    expect(filterChipsByQuery(items, 'zzz')).toEqual([]);
  });

  it('tolerates non-array input and missing names', () => {
    expect(filterChipsByQuery(null, 'x')).toEqual([]);
    expect(filterChipsByQuery([{}, { name: 'ok' }], 'ok')).toEqual([
      { name: 'ok' },
    ]);
  });
});

describe('countAllowed', () => {
  it('counts only the allowed items', () => {
    expect(
      countAllowed([
        { name: 'a', allowed: true },
        { name: 'b', allowed: false },
        { name: 'c', allowed: true },
      ]),
    ).toBe(2);
  });

  it('returns 0 for an empty or non-array input', () => {
    expect(countAllowed([])).toBe(0);
    expect(countAllowed(undefined)).toBe(0);
  });
});
