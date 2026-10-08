import { describe, expect, it } from 'vitest';

import {
  buildRrule,
  emptyEventForm,
  eventFormPayload,
  eventFormProblem,
  eventFormValues,
  monthlyChoices,
  moveEventStart,
  occurrenceFormValues,
  parseRrule,
} from '../calendarEventForm.js';

function event(fields) {
  return {
    id: 'evt-1',
    title: 'Review',
    description: null,
    location: null,
    all_day: false,
    tz_name: 'UTC',
    rrule: null,
    ...fields,
  };
}

describe('calendar event form', () => {
  it('shows a timed event on the server wall clock and an all-day end as its last day', () => {
    // 09:00 in Berlin summer time is 07:00 UTC, the server zone here.
    expect(
      eventFormValues(
        event({
          start: '2026-09-07T09:00:00',
          end: '2026-09-07T10:30:00',
          tz_name: 'Europe/Berlin',
        }),
        'UTC',
      ),
    ).toMatchObject({
      all_day: false,
      start_date: '2026-09-07',
      start_time: '07:00',
      end_date: '2026-09-07',
      end_time: '08:30',
      freq: 'none',
    });
    // The stored end of an all-day event is exclusive.
    expect(
      eventFormValues(
        event({
          all_day: true,
          tz_name: null,
          start: '2026-09-05',
          end: '2026-09-08',
        }),
        'Europe/Berlin',
      ),
    ).toMatchObject({
      all_day: true,
      start_date: '2026-09-05',
      end_date: '2026-09-07',
    });
    // One occurrence shows its own instants in the server zone.
    expect(
      occurrenceFormValues(
        {
          title: 'Moved',
          description: 'Bring notes',
          location: 'Room 2',
          all_day: false,
          start_utc: '2026-09-07T07:00:00+00:00',
          end_utc: '2026-09-07T08:00:00+00:00',
        },
        'Europe/Berlin',
      ),
    ).toMatchObject({
      title: 'Moved',
      location: 'Room 2',
      start_time: '09:00',
      end_time: '10:00',
      freq: 'none',
    });
  });

  it('sends naive times, an exclusive all-day end and the rule of a series only', () => {
    const timed = {
      ...emptyEventForm('2026-09-07'),
      title: ' Review ',
      location: 'Room 2',
      freq: 'daily',
    };
    expect(eventFormPayload(timed)).toEqual({
      title: 'Review',
      description: null,
      location: 'Room 2',
      start: '2026-09-07T09:00:00',
      end: '2026-09-07T10:00:00',
      rrule: 'FREQ=DAILY',
    });
    // One occurrence takes no rule.
    expect(eventFormPayload(timed, { repeats: false })).not.toHaveProperty(
      'rrule',
    );

    const allDay = {
      ...emptyEventForm('2026-09-05'),
      title: 'Trip',
      all_day: true,
      end_date: '2026-09-07',
    };
    // A series without repetition clears a stored rule.
    expect(eventFormPayload(allDay)).toMatchObject({
      start: '2026-09-05',
      end: '2026-09-08',
      rrule: null,
    });
  });

  it.each([
    [
      'weekly on chosen days with a count',
      'FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=5',
      '2026-09-07',
    ],
    ['daily until a date', 'FREQ=DAILY;UNTIL=20261231', '2026-09-07'],
    ['monthly on the nth weekday', 'FREQ=MONTHLY;BYDAY=3TU', '2026-09-15'],
    ['monthly on the last weekday', 'FREQ=MONTHLY;BYDAY=-1TU', '2026-09-29'],
    ['yearly', 'FREQ=YEARLY', '2026-09-07'],
  ])(
    'edits a rule repeating %s and writes it back unchanged',
    (_label, rule, start) => {
      const fields = parseRrule(rule, start);

      expect(fields.freq).not.toBe('custom');
      expect(buildRrule({ ...fields, start_date: start })).toBe(rule);
    },
  );

  it('keeps a rule the form cannot show as custom text', () => {
    for (const rule of [
      'FREQ=WEEKLY;BYHOUR=9,17',
      'FREQ=MONTHLY;BYDAY=MO,TU',
      // The 15th is not the start's day of the month.
      'FREQ=MONTHLY;BYMONTHDAY=15',
    ]) {
      const fields = parseRrule(rule, '2026-09-07');
      expect(fields).toMatchObject({ freq: 'custom', custom_rrule: rule });
      expect(buildRrule(fields)).toBe(rule);
    }
  });

  it('offers the monthly weekday choices of the start day', () => {
    // 2026-09-15 is the third Tuesday, 2026-09-29 the last, and 2026-09-24
    // both the fourth and the last Thursday.
    expect(monthlyChoices('2026-09-15')).toEqual(['day', 'nth']);
    expect(monthlyChoices('2026-09-29')).toEqual(['day', 'last']);
    expect(monthlyChoices('2026-09-24')).toEqual(['day', 'nth', 'last']);
  });

  it('keeps the length of an event when its start moves', () => {
    const values = {
      ...emptyEventForm('2026-09-07'),
      start_time: '23:00',
      end_time: '23:30',
    };
    moveEventStart(values, { start_time: '23:45' });
    expect(values).toMatchObject({ end_date: '2026-09-08', end_time: '00:15' });

    const days = {
      ...emptyEventForm('2026-09-05'),
      all_day: true,
      end_date: '2026-09-07',
    };
    moveEventStart(days, { start_date: '2026-09-10' });
    expect(days.end_date).toBe('2026-09-12');
  });

  it.each([
    ['a missing title', { title: ' ' }, {}, 'title'],
    ['an end before the start', { end_time: '08:00' }, {}, 'end'],
    [
      'an incomplete repetition',
      { freq: 'daily', interval: '0' },
      {},
      'recurrence',
    ],
    [
      'an end date before the start',
      { freq: 'daily', end_mode: 'until', end_until: '2026-09-01' },
      {},
      'recurrence',
    ],
    // One occurrence does not edit the repetition.
    [
      'one occurrence',
      { freq: 'daily', interval: '0' },
      { repeats: false },
      '',
    ],
  ])('names %s as the form problem', (_label, patch, options, problem) => {
    const values = {
      ...emptyEventForm('2026-09-07'),
      title: 'Review',
      ...patch,
    };

    expect(eventFormProblem(values, options)).toBe(problem);
  });
});
