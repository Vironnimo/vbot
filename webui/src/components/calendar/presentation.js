// How the Calendar names an occurrence's time and an event's repetition in
// words, for the event card and the entries' details cards. Times are shown
// in the server zone; all-day dates are zone-free day keys.

import { activeLocaleTag, t } from '$lib/i18n.js';
import {
  addDaysToKey,
  dayKeyForOccurrence,
  dayKeyToUtcDate,
} from '$lib/calendarView.js';
import { parseRrule, weekOfMonth } from '$lib/calendarEventForm.js';

const WEEKDAY_INDEX = { mo: 0, tu: 1, we: 2, th: 3, fr: 4, sa: 5, su: 6 };
const ORDINALS = {
  1: () => t('calendar.form.nthFirst'),
  2: () => t('calendar.form.nthSecond'),
  3: () => t('calendar.form.nthThird'),
  4: () => t('calendar.form.nthFourth'),
};

function dateFormat(locale, options) {
  return new Intl.DateTimeFormat(locale, { timeZone: 'UTC', ...options });
}

// The year is named only outside the current one.
function dayOptions(dayKey, currentDayKey, { weekday = 'long' } = {}) {
  return {
    ...(weekday ? { weekday } : {}),
    month: 'long',
    day: 'numeric',
    ...(dayKey.slice(0, 4) === currentDayKey.slice(0, 4)
      ? {}
      : { year: 'numeric' }),
  };
}

/**
 * When an occurrence takes place, with its date: "Wednesday, September 23 ·
 * 9:00 – 9:30 AM", "Wednesday, September 23 · All day", "September 23 – 25 ·
 * All day", or a range of date and time for a timed event over midnight.
 */
export function occurrenceWhen(
  occurrence,
  { timeZone = 'UTC', currentDayKey, locale = activeLocaleTag() } = {},
) {
  if (occurrence.all_day) {
    const first = String(occurrence.start).slice(0, 10);
    const last = addDaysToKey(String(occurrence.end).slice(0, 10), -1);
    const date =
      last > first
        ? dateFormat(
            locale,
            dayOptions(first, currentDayKey, { weekday: '' }),
          ).formatRange(dayKeyToUtcDate(first), dayKeyToUtcDate(last))
        : dateFormat(locale, dayOptions(first, currentDayKey)).format(
            dayKeyToUtcDate(first),
          );
    return t('calendar.detail.allDayOn', { date });
  }
  const start = new Date(occurrence.start_utc);
  const end = new Date(occurrence.end_utc);
  const firstDay = dayKeyForOccurrence(occurrence, timeZone);
  const lastDay = dayKeyForOccurrence(
    { all_day: false, start_utc: occurrence.end_utc },
    timeZone,
  );
  const time = { hour: 'numeric', minute: '2-digit', timeZone };
  if (firstDay !== lastDay) {
    return new Intl.DateTimeFormat(locale, {
      ...dayOptions(firstDay, currentDayKey, { weekday: 'short' }),
      month: 'short',
      ...time,
    }).formatRange(start, end);
  }
  return t('calendar.detail.dateTime', {
    date: dateFormat(locale, dayOptions(firstDay, currentDayKey)).format(
      dayKeyToUtcDate(firstDay),
    ),
    time: new Intl.DateTimeFormat(locale, time).formatRange(start, end),
  });
}

/**
 * A monthly repetition of a start day as a choice: "Day 15", "The third
 * Tuesday", "The last Tuesday".
 */
export function monthlyLabel(choice, dayKey, locale = activeLocaleTag()) {
  const { day, nth, weekday } = monthlyParts(choice, dayKey, locale);
  return choice === 'day'
    ? t('calendar.form.monthlyDay', { day })
    : t('calendar.form.monthlyWeekday', { nth, weekday });
}

function monthlyParts(choice, dayKey, locale) {
  return {
    day: Number(String(dayKey).slice(8, 10)),
    nth:
      choice === 'last'
        ? t('calendar.form.nthLast')
        : (ORDINALS[weekOfMonth(dayKey)]?.() ?? ''),
    weekday: dateFormat(locale, { weekday: 'long' }).format(
      dayKeyToUtcDate(dayKey),
    ),
  };
}

function weekdayList(codes, locale) {
  const format = dateFormat(locale, { weekday: 'long' });
  // 2023-01-02 is a Monday.
  const names = codes.map((code) =>
    format.format(new Date(Date.UTC(2023, 0, 2 + WEEKDAY_INDEX[code]))),
  );
  return new Intl.ListFormat(locale, { type: 'conjunction' }).format(names);
}

function ruleText(fields, startKey, locale) {
  const count = Number(fields.interval) || 1;
  const every = count > 1;
  switch (fields.freq) {
    case 'daily':
      return every
        ? t('calendar.repeat.everyDays', { count })
        : t('calendar.repeat.daily');
    case 'weekly': {
      const days = weekdayList(fields.by_weekday, locale);
      return every
        ? t('calendar.repeat.everyWeeks', { count, days })
        : t('calendar.repeat.weekly', { days });
    }
    case 'monthly': {
      const { day, nth, weekday } = monthlyParts(
        fields.monthly_by,
        startKey,
        locale,
      );
      if (fields.monthly_by === 'day') {
        return every
          ? t('calendar.repeat.everyMonthsDay', { count, day })
          : t('calendar.repeat.monthlyDay', { day });
      }
      return every
        ? t('calendar.repeat.everyMonthsWeekday', { count, nth, weekday })
        : t('calendar.repeat.monthlyWeekday', { nth, weekday });
    }
    case 'yearly': {
      const date = dateFormat(locale, { month: 'long', day: 'numeric' }).format(
        dayKeyToUtcDate(startKey),
      );
      return every
        ? t('calendar.repeat.everyYears', { count, date })
        : t('calendar.repeat.yearly', { date });
    }
    default:
      return '';
  }
}

/**
 * How an event repeats, in words: "Weekly on Wednesday", "Every 2 weeks on
 * Monday and Thursday, 10 times", "Monthly on the third Tuesday, until
 * December 1, 2026". A rule the form cannot describe reads "Repeating event".
 */
export function recurrenceSummary(event, locale = activeLocaleTag()) {
  const startKey = String(event?.start ?? '').slice(0, 10);
  const fields = parseRrule(event?.rrule, startKey);
  const rule = ruleText(fields, startKey, locale);
  if (!rule) return t('calendar.detail.recurring');
  if (fields.end_mode === 'count') {
    return t('calendar.repeat.count', { rule, count: fields.end_count });
  }
  if (fields.end_mode === 'until' && fields.end_until) {
    const date = dateFormat(locale, {
      month: 'long',
      day: 'numeric',
      year: 'numeric',
    }).format(dayKeyToUtcDate(fields.end_until));
    return t('calendar.repeat.until', { rule, date });
  }
  return rule;
}
