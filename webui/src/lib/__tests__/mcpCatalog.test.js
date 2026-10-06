import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

import { mcpCatalogMark } from '../mcpCatalog.js';

const { entries } = JSON.parse(
  readFileSync(
    new URL(
      '../../../../resources/extensions/mcp/catalog.json',
      import.meta.url,
    ),
    'utf8',
  ),
);

// WCAG 2 contrast of white against an `hsl(h s% l%)` color.
function whiteContrast(color) {
  const [, h, s, l] = color
    .match(/^hsl\((\d+(?:\.\d+)?) (\d+(?:\.\d+)?)% (\d+(?:\.\d+)?)%\)$/)
    .map(Number);
  const a = (s / 100) * Math.min(l / 100, 1 - l / 100);
  const channel = (n) => {
    const k = (n + h / 30) % 12;
    const value = l / 100 - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  const luminance =
    0.2126 * channel(0) + 0.7152 * channel(8) + 0.0722 * channel(4);
  return 1.05 / (luminance + 0.05);
}

describe('mcpCatalogMark()', () => {
  it('keeps every catalog service mark readable: white initials at 4.5:1 or more', () => {
    expect(entries.length).toBeGreaterThan(0);
    for (const entry of entries) {
      const mark = mcpCatalogMark(entry);
      expect(mark.initials, entry.id).toMatch(/^\S{1,2}$/);
      expect(whiteContrast(mark.background), entry.id).toBeGreaterThanOrEqual(
        4.5,
      );
    }
  });
});
