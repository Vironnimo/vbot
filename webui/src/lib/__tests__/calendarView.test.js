import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  addDaysToKey,
  createCalendarController,
  createCalendarViewState,
  dayKeyForOccurrence,
  dayKeyInZone,
  eventToFormValues,
  formatTimeInZone,
  formValuesToPayload,
  groupByDay,
  monthGridDays,
  navigateAnchor,
  sortDayEntries,
  weekStartKey,
  windowForView,
} from '../calendarView.js';

import { addCalendarExdate, updateCalendarEvent } from '../api.js';

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

describe('day key helpers', () => {
  it('adds days across month boundaries', () => {
    expect(addDaysToKey('2026-08-31', 1)).toBe('2026-09-01');
    expect(addDaysToKey('2026-09-01', -1)).toBe('2026-08-31');
  });

  it('computes monday-first weekday index', () => {
    // 2026-08-31 is a Monday.
    expect(weekStartKey('2026-08-31')).toBe('2026-08-31');
    // 2026-09-06 is a Sunday in the same week.
    expect(weekStartKey('2026-09-06')).toBe('2026-08-31');
  });

  it('builds a six-week month grid around the month', () => {
    const days = monthGridDays('2026-09-15');
    expect(days).toHaveLength(42);
    expect(days[0].key).toBe('2026-08-31');
    expect(days.filter((day) => day.inMonth)).toHaveLength(30);
  });

  it('marks today inside the grid', () => {
    const days = monthGridDays(todayKey());
    expect(days.some((day) => day.isToday)).toBe(true);
  });
});

describe('windowForView', () => {
  it('covers the whole rendered six-week grid for month view', () => {
    // The grid shows surrounding days from the adjacent months too, so the
    // request window must span the full 42-day grid, not just the month.
    expect(windowForView('month', '2026-09-15')).toEqual({
      from: '2026-08-31',
      to: '2026-10-11',
    });
  });

  it('covers monday to sunday for week view', () => {
    expect(windowForView('week', '2026-09-02')).toEqual({
      from: '2026-08-31',
      to: '2026-09-06',
    });
  });

  it('covers a single day for day view', () => {
    expect(windowForView('day', '2026-09-02')).toEqual({
      from: '2026-09-02',
      to: '2026-09-02',
    });
  });

  it('starts today for the agenda', () => {
    const window = windowForView('agenda', '2020-01-01');
    expect(window.from).toBe(todayKey());
    expect(addDaysToKey(window.from, 13)).toBe(window.to);
  });
});

describe('navigateAnchor', () => {
  it('steps months from the first of the month', () => {
    expect(navigateAnchor('month', '2026-09-15', 1)).toBe('2026-10-01');
    expect(navigateAnchor('month', '2026-09-15', -1)).toBe('2026-08-01');
  });

  it('steps weeks and days', () => {
    expect(navigateAnchor('week', '2026-09-02', 1)).toBe('2026-09-09');
    expect(navigateAnchor('day', '2026-09-30', 1)).toBe('2026-10-01');
  });
});

describe('server timezone rendering', () => {
  it('maps a UTC instant to its local day key', () => {
    // 22:00 UTC is already the next day in Europe/Berlin.
    expect(dayKeyInZone('2026-09-02T22:00:00+00:00', 'Europe/Berlin')).toBe(
      '2026-09-03',
    );
    expect(dayKeyInZone('2026-09-02T21:59:00+00:00', 'Europe/Berlin')).toBe(
      '2026-09-02',
    );
  });

  it('formats times in the server zone', () => {
    // 07:00 UTC is 09:00 in Europe/Berlin summer time.
    expect(
      formatTimeInZone('2026-09-03T07:00:00+00:00', 'Europe/Berlin', 'en-GB'),
    ).toBe('09:00');
  });

  it('groups occurrences by their local day', () => {
    const grouped = groupByDay(
      [
        {
          title: 'a',
          all_day: false,
          start_utc: '2026-09-02T22:00:00+00:00',
          start_date: '',
        },
        {
          title: 'b',
          all_day: true,
          start_utc: null,
          start_date: '2026-09-05',
        },
      ],
      (occurrence) => dayKeyForOccurrence(occurrence, 'Europe/Berlin'),
    );
    expect(grouped['2026-09-03']).toHaveLength(1);
    expect(grouped['2026-09-05']).toHaveLength(1);
  });
});

describe('sortDayEntries', () => {
  it('lists all-day entries first, then by time', () => {
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
});

describe('event form mapping', () => {
  it('projects a recurring timed event into editable values and back', () => {
    const values = eventToFormValues({
      title: 'Standup',
      notes: null,
      location: null,
      all_day: false,
      start_utc: null,
      start_local: '2026-08-31T09:00:00',
      tz_name: 'Europe/Berlin',
      start_date: null,
      duration_minutes: 30,
      duration_days: null,
      rrule: {
        freq: 'weekly',
        interval: 2,
        count: 5,
        until: null,
        by_weekday: ['mo', 'we'],
      },
      exdates: [],
    });
    expect(values.start_date).toBe('2026-08-31');
    expect(values.start_time).toBe('09:00');
    expect(values.freq).toBe('weekly');
    expect(values.end_mode).toBe('count');

    const payload = formValuesToPayload(values);
    expect(payload.rrule).toEqual({
      freq: 'weekly',
      interval: 2,
      by_weekday: ['mo', 'we'],
      count: 5,
    });
  });

  it('renders a single timed event in the server zone, not raw UTC', () => {
    const values = eventToFormValues(
      {
        title: 'Zahnarzt',
        notes: null,
        location: null,
        all_day: false,
        start_utc: '2026-09-07T07:00:00+00:00',
        start_local: null,
        tz_name: null,
        start_date: null,
        duration_minutes: 60,
        duration_days: null,
        rrule: null,
        exdates: [],
      },
      'Europe/Berlin',
    );
    // 07:00 UTC is 09:00 in Berlin; the form presents that wall clock.
    expect(values.start_date).toBe('2026-09-07');
    expect(values.start_time).toBe('09:00');
  });

  it('resaving an edited single event keeps its wall clock', () => {
    const values = eventToFormValues(
      {
        title: 'Zahnarzt',
        notes: null,
        location: null,
        all_day: false,
        start_utc: '2026-09-07T07:00:00+00:00',
        start_local: null,
        tz_name: null,
        start_date: null,
        duration_minutes: 60,
        duration_days: null,
        rrule: null,
        exdates: [],
      },
      'Europe/Berlin',
    );
    const payload = formValuesToPayload(values);
    // Regression: slicing start_utc naively produced the wrong wall clock, so a
    // save without edits shifted the event. The roundtrip must stay 09:00 Berlin.
    expect(payload.start).toBe('2026-09-07T09:00:00');
  });

  it('rolls a late single event into the next server day', () => {
    const values = eventToFormValues(
      {
        title: 'Late',
        notes: null,
        location: null,
        all_day: false,
        start_utc: '2026-09-02T22:00:00+00:00',
        start_local: null,
        tz_name: null,
        start_date: null,
        duration_minutes: 60,
        duration_days: null,
        rrule: null,
        exdates: [],
      },
      'Europe/Berlin',
    );
    // 22:00 UTC is already 00:00 on 2026-09-03 in Berlin.
    expect(values.start_date).toBe('2026-09-03');
    expect(values.start_time).toBe('00:00');
  });

  it('clears recurrence when freq is none', () => {
    const payload = formValuesToPayload({
      title: 'X',
      notes: '',
      location: '',
      all_day: false,
      start_date: '2026-09-03',
      start_time: '15:00',
      duration_minutes: 30,
      duration_days: 1,
      tz: '',
      freq: 'none',
      interval: 1,
      by_weekday: [],
      end_mode: 'never',
      end_count: 10,
      end_until: '',
    });
    expect(payload.rrule).toBeNull();
    expect(payload.start).toBe('2026-09-03T15:00:00');
    expect(payload.tz).toBeUndefined();
  });
});

describe('controller', () => {
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

  it('excludes an occurrence additively instead of replacing exdates', async () => {
    const state = createCalendarViewState();
    const controller = createCalendarController({ state });

    await controller.excludeOccurrence('evt-1', '2026-09-14T09:00:00');

    expect(addCalendarExdate).toHaveBeenCalledWith({
      id: 'evt-1',
      occurrence_start: '2026-09-14T09:00:00',
    });
    // Regression: previously this read all exdates and re-sent the whole
    // array via update, which could drop a concurrent tab's exclusion.
    expect(updateCalendarEvent).not.toHaveBeenCalled();
  });
});

function todayKey() {
  const now = new Date();
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, '0')}-${String(now.getUTCDate()).padStart(2, '0')}`;
}
