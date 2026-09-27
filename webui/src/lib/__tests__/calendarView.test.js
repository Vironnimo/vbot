import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  addDaysToKey,
  createCalendarController,
  createCalendarViewState,
  dayKeyForOccurrence,
  eventToFormValues,
  formatTimeInZone,
  groupByDay,
  monthGridDays,
  sortDayEntries,
  todayKey,
  weekColumnLabel,
  weekRangeLabel,
  weekStartKey,
  windowForView,
} from '../calendarView.js';

import { getCalendarWindow } from '../api.js';

vi.mock('../api.js', () => ({
  getCalendarWindow: vi.fn(() =>
    Promise.resolve({
      events: [],
      occurrences: [],
      cron: [],
      system_timezone: 'UTC',
    }),
  ),
  createCalendarEvent: vi.fn(() => Promise.resolve({})),
  updateCalendarEvent: vi.fn(() => Promise.resolve({})),
  deleteCalendarEvent: vi.fn(() => Promise.resolve({})),
  addCalendarExdate: vi.fn(() => Promise.resolve({})),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

function serverWindow(systemTimezone) {
  return {
    events: [],
    occurrences: [],
    cron: [],
    system_timezone: systemTimezone,
  };
}

describe('day keys and the month grid', () => {
  it('computes zero-padded Monday-first day keys across month boundaries', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-01T00:30:00Z'));

    expect(todayKey()).toBe('2026-09-01');
    // The server zone decides the current day, not UTC.
    expect(todayKey('America/Los_Angeles')).toBe('2026-08-31');
    expect(addDaysToKey('2026-08-31', 1)).toBe('2026-09-01');
    expect(addDaysToKey('2026-09-01', -1)).toBe('2026-08-31');
    // 2026-08-31 is a Monday; 2026-09-06 is the Sunday of the same week.
    expect(weekStartKey('2026-08-31')).toBe('2026-08-31');
    expect(weekStartKey('2026-09-06')).toBe('2026-08-31');
  });

  it('builds a six-week month grid marking the month and today', () => {
    const days = monthGridDays('2026-09-15', '2026-09-23');

    expect(days).toHaveLength(42);
    expect(days[0].key).toBe('2026-08-31');
    expect(days.filter((day) => day.inMonth)).toHaveLength(30);
    expect(days.filter((day) => day.isToday).map((day) => day.key)).toEqual([
      '2026-09-23',
    ]);
  });

  it('labels the week range once with its years and columns compactly', () => {
    // Intl separates ranges with (thin) spaces around an en dash; normalize
    // whitespace so the assertions describe the content, not ICU spacing.
    const plain = (text) => text.replace(/\s+/g, ' ');

    expect(plain(weekRangeLabel('2026-09-23', 'en'))).toBe('Sep 21 – 27, 2026');
    expect(plain(weekRangeLabel('2026-10-01', 'en'))).toBe(
      'Sep 28 – Oct 4, 2026',
    );
    expect(plain(weekRangeLabel('2026-12-31', 'en'))).toBe(
      'Dec 28, 2026 – Jan 3, 2027',
    );
    expect(weekColumnLabel('2026-09-21', 'en')).toEqual({
      weekday: 'Mon',
      dayOfMonth: 21,
    });
  });

  it('requests exactly the rendered days of each view', () => {
    // The month grid shows surrounding days from the adjacent months too, so
    // the request window spans the full 42-day grid, not just the month.
    expect(windowForView('month', '2026-09-15')).toEqual({
      from: '2026-08-31',
      to: '2026-10-11',
    });
    expect(windowForView('week', '2026-09-02')).toEqual({
      from: '2026-08-31',
      to: '2026-09-06',
    });
    expect(windowForView('day', '2026-09-02')).toEqual({
      from: '2026-09-02',
      to: '2026-09-02',
    });
    expect(windowForView('agenda', '2020-01-01')).toEqual({
      from: '2020-01-01',
      to: '2020-01-14',
    });
  });
});

describe('server timezone rendering', () => {
  it('groups occurrences by their day in the server timezone', () => {
    const late = {
      title: 'late',
      all_day: false,
      // 22:00 UTC is already the next day in Europe/Berlin.
      start_utc: '2026-09-02T22:00:00+00:00',
      start_date: '',
    };
    const evening = {
      title: 'evening',
      all_day: false,
      start_utc: '2026-09-02T21:59:00+00:00',
      start_date: '',
    };
    const allDay = {
      title: 'all day',
      all_day: true,
      start_utc: null,
      start_date: '2026-09-05',
    };

    expect(
      groupByDay([late, evening, allDay], (occurrence) =>
        dayKeyForOccurrence(occurrence, 'Europe/Berlin'),
      ),
    ).toEqual({
      '2026-09-02': [evening],
      '2026-09-03': [late],
      '2026-09-05': [allDay],
    });
    // 07:00 UTC is 09:00 in Europe/Berlin summer time.
    expect(
      formatTimeInZone('2026-09-03T07:00:00+00:00', 'Europe/Berlin', 'en-GB'),
    ).toBe('09:00');
  });

  it('lists all-day entries first, then cron, then by time', () => {
    const sorted = sortDayEntries([
      {
        all_day: false,
        start_utc: '2026-09-02T10:00:00+00:00',
        fire_at: '',
        title: 'late',
      },
      { all_day: true, start_utc: null, fire_at: '', title: 'allday' },
      {
        all_day: false,
        start_utc: '',
        fire_at: '2026-09-02T07:00:00+00:00',
        title: 'cron',
      },
      {
        all_day: false,
        start_utc: '2026-09-02T08:00:00+00:00',
        fire_at: '',
        title: 'early',
      },
    ]);
    expect(sorted.map((entry) => entry.title)).toEqual([
      'allday',
      'cron',
      'early',
      'late',
    ]);
  });

  it.each([
    [
      'a recurring timed event from its local start',
      {
        start_utc: null,
        start_local: '2026-08-31T09:00:00',
        tz_name: 'Europe/Berlin',
        duration_minutes: 30,
        rrule: {
          freq: 'weekly',
          interval: 2,
          count: 5,
          until: null,
          by_weekday: ['mo', 'we'],
        },
      },
      {
        start_date: '2026-08-31',
        start_time: '09:00',
        duration_minutes: 30,
        freq: 'weekly',
        interval: 2,
        by_weekday: ['mo', 'we'],
        end_mode: 'count',
        end_count: 5,
      },
    ],
    [
      'a single timed event in the server zone, not raw UTC',
      // 07:00 UTC is 09:00 in Berlin; the form presents that wall clock.
      { start_utc: '2026-09-07T07:00:00+00:00' },
      {
        start_date: '2026-09-07',
        start_time: '09:00',
        freq: 'none',
        end_mode: 'never',
      },
    ],
    [
      'a late single event on the next server day',
      // 22:00 UTC is already 00:00 on 2026-09-03 in Berlin.
      { start_utc: '2026-09-02T22:00:00+00:00' },
      { start_date: '2026-09-03', start_time: '00:00' },
    ],
  ])('projects %s into editable values', (_label, fields, expected) => {
    const values = eventToFormValues(
      {
        title: 'Event',
        notes: null,
        location: null,
        all_day: false,
        start_utc: null,
        start_local: null,
        tz_name: null,
        start_date: null,
        duration_minutes: 60,
        duration_days: null,
        rrule: null,
        exdates: [],
        ...fields,
      },
      'Europe/Berlin',
    );

    expect(values).toMatchObject(expected);
  });
});

describe('controller', () => {
  it('corrects the initial anchor to the server day and keeps Today in the server timezone', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-01T00:30:00Z'));
    getCalendarWindow.mockResolvedValue(serverWindow('America/Los_Angeles'));
    const state = createCalendarViewState();
    const controller = createCalendarController({ state });

    await controller.load();

    expect(state.anchorKey).toBe('2026-08-31');
    expect(getCalendarWindow).toHaveBeenLastCalledWith({
      from: '2026-07-27',
      to: '2026-09-06',
    });

    state.anchorKey = '2026-10-15';
    controller.goToday();
    expect(state.anchorKey).toBe('2026-08-31');
  });

  it('steps the anchor by the period of the current view and loads its window', async () => {
    const state = createCalendarViewState();
    state.anchorKey = '2026-09-15';
    const controller = createCalendarController({ state });

    controller.navigate(1);
    expect(state.anchorKey).toBe('2026-10-01');
    expect(getCalendarWindow).toHaveBeenLastCalledWith(
      windowForView('month', '2026-10-01'),
    );
    controller.navigate(-2);
    expect(state.anchorKey).toBe('2026-08-01');

    controller.setAnchor('2026-09-02');
    controller.setView('week');
    controller.navigate(1);
    expect(state.anchorKey).toBe('2026-09-09');
    expect(getCalendarWindow).toHaveBeenLastCalledWith({
      from: '2026-09-07',
      to: '2026-09-13',
    });

    controller.setAnchor('2026-09-30');
    controller.setView('day');
    controller.navigate(1);
    expect(state.anchorKey).toBe('2026-10-01');
  });

  it('toggles layers', () => {
    const state = createCalendarViewState();
    const controller = createCalendarController({ state });
    controller.toggleLayer('local');
    expect(state.showLocalLayer).toBe(false);
    controller.toggleLayer('cron');
    expect(state.showCronLayer).toBe(false);
    controller.toggleLayer('local');
    expect(state.showLocalLayer).toBe(true);
  });
});
