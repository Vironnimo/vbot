import { describe, expect, it } from 'vitest';
import { initialInputDrafts, zonedDateTime } from '../extensionInputs.js';

describe('Extension input date and time', () => {
  it.each([
    ['2026-07-01T09:30', 'Europe/Berlin', '2026-07-01T09:30:00+02:00'],
    ['2026-01-15T09:30:15', 'Europe/Berlin', '2026-01-15T09:30:15+01:00'],
    ['2026-01-15T09:30', 'America/St_Johns', '2026-01-15T09:30:00-03:30'],
    ['2026-01-15', 'UTC', ''],
  ])('reads the wall time %s in %s as %s', (text, timeZone, expected) => {
    expect(zonedDateTime(text, timeZone)).toBe(expected);
  });

  it('shows a default instant as wall time in the app time zone', () => {
    const request = {
      kind: 'elicitation',
      payload: {
        requestedSchema: {
          properties: {
            when: {
              type: 'string',
              format: 'date-time',
              default: '2026-07-01T07:30:00Z',
            },
          },
        },
      },
    };
    expect(initialInputDrafts(request, { timeZone: 'Europe/Berlin' })).toEqual({
      when: '2026-07-01T09:30',
    });
  });
});
