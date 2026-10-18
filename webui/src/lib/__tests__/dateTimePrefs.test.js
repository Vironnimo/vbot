import { afterEach, describe, expect, it } from 'vitest';

import {
  dateKeyInApplicationZone,
  dateTimePartsInZone,
  timeZoneOffsetMilliseconds,
  formatDateTimeInApplicationZone,
  setApplicationTimeZone,
} from '../dateTimePrefs.svelte.js';

describe('application timezone preferences', () => {
  afterEach(() => setApplicationTimeZone('UTC'));

  it('formats the same instant in the configured IANA zone', () => {
    const format = () =>
      formatDateTimeInApplicationZone('2026-01-15T08:00:00Z', 'en-GB', {
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      });
    setApplicationTimeZone('Europe/Berlin');
    expect(format()).toBe('09:00');

    setApplicationTimeZone('Asia/Tokyo');
    expect(format()).toBe('17:00');
  });

  it('derives date keys in the configured zone around midnight', () => {
    setApplicationTimeZone('America/New_York');
    expect(dateKeyInApplicationZone('2026-01-01T02:00:00Z')).toBe('2025-12-31');

    setApplicationTimeZone('UTC');
    expect(dateKeyInApplicationZone('2026-01-01T02:00:00Z')).toBe('2026-01-01');
  });

  it.each([
    ['Europe/Berlin', '2026-03-29T00:59:59.987Z', 3_600_000, '01'],
    ['Europe/Berlin', '2026-03-29T01:00:00.987Z', 7_200_000, '03'],
    ['Asia/Kathmandu', '2026-03-29T01:00:00.987Z', 20_700_000, '06'],
  ])(
    'projects explicit-zone parts and offsets across transitions',
    (zone, iso, offset, hour) => {
      const instant = Date.parse(iso);
      setApplicationTimeZone('America/New_York');
      expect(dateTimePartsInZone(instant, zone).hour).toBe(hour);
      expect(timeZoneOffsetMilliseconds(instant, zone)).toBe(offset);
      expect(() => dateTimePartsInZone(instant, 'Unsupported/Zone')).toThrow(
        RangeError,
      );
    },
  );

  it('falls back to UTC for an unsupported zone', () => {
    setApplicationTimeZone('Mars/Olympus');

    expect(dateKeyInApplicationZone('2026-01-01T02:00:00Z')).toBe('2026-01-01');
  });
});
