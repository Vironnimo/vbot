import { beforeEach, describe, expect, it } from 'vitest';

import { init } from '../i18n.js';
import { setApplicationTimeZone } from '../dateTimePrefs.svelte.js';
import {
  formatAbsoluteTime,
  formatMoment,
  formatRelativeTime,
} from '../timeText.js';

const NOW = Date.parse('2026-09-29T15:16:00Z');

describe('time text', () => {
  beforeEach(() => {
    init('en');
    setApplicationTimeZone('Europe/Berlin');
  });

  it.each([
    ['2026-09-29T15:15:30Z', 'now'],
    ['2026-09-29T15:04:00Z', '12 minutes ago'],
    ['2026-09-29T12:40:00Z', '3 hours ago'],
    ['2026-09-28T15:00:00Z', 'yesterday'],
    ['2026-09-29T18:16:00Z', 'in 3 hours'],
    ['2026-07-01T09:00:00Z', '3 months ago'],
    ['not a time', ''],
    [null, ''],
  ])('describes %s relative to now as %j', (value, expected) => {
    expect(formatRelativeTime(value, NOW)).toBe(expected);
  });

  it('pairs the moment in the application time zone with its distance', () => {
    const berlin = new Intl.DateTimeFormat('en', {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: 'Europe/Berlin',
    }).format(new Date('2026-09-29T15:04:00Z'));

    expect(formatAbsoluteTime('2026-09-29T15:04:00Z')).toBe(berlin);
    expect(
      formatMoment(Date.parse('2026-09-29T15:04:00Z'), { nowMs: NOW }),
    ).toBe(`${berlin} · 12 minutes ago`);
    expect(formatMoment('', { nowMs: NOW })).toBe('');
  });
});
