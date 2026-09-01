import { afterEach, describe, expect, it } from 'vitest';

import {
  dateKeyInApplicationZone,
  formatDateTimeInApplicationZone,
  setApplicationTimeZone,
} from '../dateTimePrefs.svelte.js';

describe('application timezone preferences', () => {
  afterEach(() => setApplicationTimeZone('UTC'));

  it('formats the same instant in the configured IANA zone', () => {
    setApplicationTimeZone('Europe/Berlin');

    expect(
      formatDateTimeInApplicationZone('2026-01-15T08:00:00Z', 'en-GB', {
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      }),
    ).toBe('09:00');
  });

  it('derives date keys in the configured zone around midnight', () => {
    setApplicationTimeZone('America/New_York');

    expect(dateKeyInApplicationZone('2026-01-01T02:00:00Z')).toBe('2025-12-31');
  });

  it('falls back to UTC for an unsupported zone', () => {
    setApplicationTimeZone('Mars/Olympus');

    expect(dateKeyInApplicationZone('2026-01-01T02:00:00Z')).toBe('2026-01-01');
  });
});
