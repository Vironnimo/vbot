// The Calendar's event form: an event or one occurrence as editable values,
// and those values as a `calendar.create` or `calendar.update` payload.
//
// The form edits the server-zone wall clock the grid shows. A timed event
// stores naive local times in its own zone (`tz_name`, the server zone when it
// was made); an all-day event stores dates with an exclusive end, which the
// form shows inclusively as its last day. Repetition is an RFC 5545 RRULE: the
// form edits the common shapes (every N days, weeks on chosen weekdays,
// months on a day or weekday, years; ending never, after a count or on a
// date) and keeps any other rule unchanged as custom text.

import { addDaysToKey, isDayKey } from './calendarView.js';

export const WEEKDAY_CODES = ['mo', 'tu', 'we', 'th', 'fr', 'sa', 'su'];
const FREQUENCIES = {
  DAILY: 'daily',
  WEEKLY: 'weekly',
  MONTHLY: 'monthly',
  YEARLY: 'yearly',
};
const FORM_PARTS = new Set([
  'FREQ',
  'INTERVAL',
  'COUNT',
  'UNTIL',
  'BYDAY',
  'BYMONTHDAY',
]);

function recurrenceDefaults() {
  return {
    freq: 'none',
    interval: '1',
    by_weekday: [],
    monthly_by: 'day',
    end_mode: 'never',
    end_count: '10',
    end_until: '',
    custom_rrule: '',
  };
}

// A new event's values: one hour from 09:00 on `dayKey`, not repeating.
export function emptyEventForm(dayKey) {
  return {
    title: '',
    description: '',
    location: '',
    all_day: false,
    start_date: dayKey,
    start_time: '09:00',
    end_date: dayKey,
    end_time: '10:00',
    ...recurrenceDefaults(),
  };
}

// A whole event's values, its times in the server zone.
export function eventFormValues(event, systemTimeZone = 'UTC') {
  const allDay = Boolean(event.all_day);
  const times = allDay
    ? allDayTimes(event.start, event.end)
    : {
        ...wallClockFields(
          'start',
          zoneInstant(event.start, event.tz_name || systemTimeZone),
          systemTimeZone,
        ),
        ...wallClockFields(
          'end',
          zoneInstant(event.end, event.tz_name || systemTimeZone),
          systemTimeZone,
        ),
      };
  return {
    title: event.title ?? '',
    description: event.description ?? '',
    location: event.location ?? '',
    all_day: allDay,
    ...defaultTimes(allDay),
    ...times,
    ...parseRrule(event.rrule, times.start_date),
  };
}

// One occurrence's values: its own title, place, notes and times.
export function occurrenceFormValues(occurrence, systemTimeZone = 'UTC') {
  const allDay = Boolean(occurrence.all_day);
  const times = allDay
    ? allDayTimes(occurrence.start, occurrence.end)
    : {
        ...wallClockFields(
          'start',
          Date.parse(occurrence.start_utc),
          systemTimeZone,
        ),
        ...wallClockFields(
          'end',
          Date.parse(occurrence.end_utc),
          systemTimeZone,
        ),
      };
  return {
    title: occurrence.title ?? '',
    description: occurrence.description ?? '',
    location: occurrence.location ?? '',
    all_day: allDay,
    ...defaultTimes(allDay),
    ...times,
    ...recurrenceDefaults(),
  };
}

function defaultTimes(allDay) {
  return allDay ? { start_time: '09:00', end_time: '10:00' } : {};
}

// All-day dates: the exclusive stored end is shown as the last day.
function allDayTimes(start, end) {
  const startDate = String(start ?? '').slice(0, 10);
  const lastDay = end ? addDaysToKey(String(end).slice(0, 10), -1) : startDate;
  return {
    start_date: startDate,
    end_date: lastDay < startDate ? startDate : lastDay,
  };
}

// The first problem of the values, or '' when they can be saved: 'title',
// 'date', 'end' (not after the start) or 'recurrence'. `repeats` is false for
// one occurrence, whose repetition is not edited.
export function eventFormProblem(values, { repeats = true } = {}) {
  if (!String(values.title ?? '').trim()) {
    return 'title';
  }
  if (!isDayKey(values.start_date) || !isDayKey(values.end_date)) {
    return 'date';
  }
  if (values.all_day) {
    if (values.end_date < values.start_date) {
      return 'end';
    }
  } else if (
    !isTime(values.start_time) ||
    !isTime(values.end_time) ||
    `${values.end_date}T${values.end_time}` <=
      `${values.start_date}T${values.start_time}`
  ) {
    return 'end';
  }
  if (!repeats || values.freq === 'none') {
    return '';
  }
  if (values.freq === 'custom') {
    return values.custom_rrule.trim() ? '' : 'recurrence';
  }
  if (!positiveWhole(values.interval)) {
    return 'recurrence';
  }
  if (values.end_mode === 'count' && !positiveWhole(values.end_count)) {
    return 'recurrence';
  }
  if (
    values.end_mode === 'until' &&
    (!isDayKey(values.end_until) || values.end_until < values.start_date)
  ) {
    return 'recurrence';
  }
  return '';
}

// The `calendar.create` / `calendar.update` fields of valid values. One
// occurrence (`repeats: false`) takes no rule; a whole event always sends
// one, so clearing the repetition stops it.
export function eventFormPayload(values, { repeats = true } = {}) {
  const payload = {
    title: values.title.trim(),
    description: values.description.trim() || null,
    location: values.location.trim() || null,
  };
  if (values.all_day) {
    payload.start = values.start_date;
    payload.end = addDaysToKey(values.end_date, 1);
  } else {
    payload.start = `${values.start_date}T${values.start_time}:00`;
    payload.end = `${values.end_date}T${values.end_time}:00`;
  }
  if (repeats) {
    payload.rrule = buildRrule(values);
  }
  return payload;
}

// Moves the start and keeps the event's length, as calendars do: the end
// follows a changed start date or time.
export function moveEventStart(values, patch) {
  const before = spanMinutes(values);
  Object.assign(values, patch);
  if (before === null) {
    return;
  }
  if (values.all_day) {
    values.end_date = addDaysToKey(values.start_date, before / 1440);
    return;
  }
  const start = naiveMinutes(values.start_date, values.start_time);
  if (start === null) {
    return;
  }
  const end = new Date((start + before) * 60000).toISOString();
  values.end_date = end.slice(0, 10);
  values.end_time = end.slice(11, 16);
}

function spanMinutes(values) {
  if (values.all_day) {
    if (!isDayKey(values.start_date) || !isDayKey(values.end_date)) {
      return null;
    }
    const days = Math.round(
      (Date.parse(values.end_date) - Date.parse(values.start_date)) / 86400000,
    );
    return Math.max(days, 0) * 1440;
  }
  const start = naiveMinutes(values.start_date, values.start_time);
  const end = naiveMinutes(values.end_date, values.end_time);
  return start === null || end === null ? null : Math.max(end - start, 0);
}

function naiveMinutes(day, time) {
  if (!isDayKey(day) || !isTime(time)) {
    return null;
  }
  return Date.parse(`${day}T${time}:00Z`) / 60000;
}

// ---------------------------------------------------------------------------
// RRULE
// ---------------------------------------------------------------------------

// The recurrence fields of a stored rule. A rule the form cannot show as it
// is loads as `custom`, keeping its text.
export function parseRrule(text, startDayKey) {
  const fields = recurrenceDefaults();
  const rule = String(text ?? '')
    .trim()
    .replace(/^RRULE:/i, '');
  if (!rule) {
    return fields;
  }
  const custom = { ...fields, freq: 'custom', custom_rrule: rule };
  const parts = {};
  for (const item of rule.split(';')) {
    const [key, value, ...rest] = item.split('=');
    const name = key.toUpperCase();
    if (!value || rest.length || !FORM_PARTS.has(name) || name in parts) {
      return custom;
    }
    parts[name] = value.toUpperCase();
  }
  const freq = FREQUENCIES[parts.FREQ];
  if (!freq) {
    return custom;
  }
  fields.freq = freq;
  if (parts.INTERVAL !== undefined) {
    if (!positiveWhole(parts.INTERVAL)) return custom;
    fields.interval = String(Number(parts.INTERVAL));
  }
  if (parts.COUNT !== undefined) {
    if (!positiveWhole(parts.COUNT) || parts.UNTIL !== undefined) return custom;
    fields.end_mode = 'count';
    fields.end_count = String(Number(parts.COUNT));
  } else if (parts.UNTIL !== undefined) {
    // A rule ending at a time of day is kept as it is.
    const until = /^(\d{4})(\d{2})(\d{2})$/.exec(parts.UNTIL);
    if (!until) return custom;
    fields.end_mode = 'until';
    fields.end_until = `${until[1]}-${until[2]}-${until[3]}`;
  }

  if (freq === 'weekly') {
    if (parts.BYMONTHDAY !== undefined) return custom;
    const days = parts.BYDAY
      ? parts.BYDAY.split(',').map((day) => day.toLowerCase())
      : [weekdayCode(startDayKey)];
    if (days.some((day) => !WEEKDAY_CODES.includes(day))) return custom;
    fields.by_weekday = WEEKDAY_CODES.filter((day) => days.includes(day));
    return fields;
  }
  if (freq === 'monthly') {
    if (parts.BYDAY !== undefined && parts.BYMONTHDAY !== undefined) {
      return custom;
    }
    if (parts.BYMONTHDAY !== undefined) {
      return Number(parts.BYMONTHDAY) === dayOfMonth(startDayKey)
        ? fields
        : custom;
    }
    if (parts.BYDAY !== undefined) {
      const choice = monthlyWeekdayChoice(parts.BYDAY, startDayKey);
      if (!choice) return custom;
      fields.monthly_by = choice;
    }
    return fields;
  }
  return parts.BYDAY === undefined && parts.BYMONTHDAY === undefined
    ? fields
    : custom;
}

// The rule the recurrence fields describe, or null for no repetition.
export function buildRrule(values) {
  if (values.freq === 'none') {
    return null;
  }
  if (values.freq === 'custom') {
    return values.custom_rrule.trim() || null;
  }
  const parts = [`FREQ=${values.freq.toUpperCase()}`];
  const interval = positiveWhole(values.interval);
  if (interval > 1) {
    parts.push(`INTERVAL=${interval}`);
  }
  if (values.freq === 'weekly' && values.by_weekday.length) {
    const days = WEEKDAY_CODES.filter((day) => values.by_weekday.includes(day));
    parts.push(`BYDAY=${days.join(',').toUpperCase()}`);
  }
  if (values.freq === 'monthly' && values.monthly_by !== 'day') {
    const weekday = weekdayCode(values.start_date).toUpperCase();
    const ordinal =
      values.monthly_by === 'last' ? -1 : weekOfMonth(values.start_date);
    parts.push(`BYDAY=${ordinal}${weekday}`);
  }
  if (values.end_mode === 'count') {
    parts.push(`COUNT=${positiveWhole(values.end_count)}`);
  } else if (values.end_mode === 'until') {
    parts.push(`UNTIL=${values.end_until.replaceAll('-', '')}`);
  }
  return parts.join(';');
}

// The monthly repetitions a start day offers: on its day of the month, on
// its weekday of the first four weeks ("third Tuesday"), and on the last such
// weekday when it falls in the month's last seven days.
export function monthlyChoices(startDayKey) {
  if (!isDayKey(startDayKey)) {
    return ['day'];
  }
  const choices = ['day'];
  if (weekOfMonth(startDayKey) <= 4) {
    choices.push('nth');
  }
  if (inLastWeek(startDayKey)) {
    choices.push('last');
  }
  return choices;
}

export function weekOfMonth(dayKey) {
  return Math.ceil(dayOfMonth(dayKey) / 7);
}

export function weekdayCode(dayKey) {
  if (!isDayKey(dayKey)) {
    return 'mo';
  }
  return WEEKDAY_CODES[(new Date(`${dayKey}T00:00:00Z`).getUTCDay() + 6) % 7];
}

function monthlyWeekdayChoice(byDay, startDayKey) {
  const match = /^(-1|[1-4])(MO|TU|WE|TH|FR|SA|SU)$/.exec(byDay);
  if (!match || match[2].toLowerCase() !== weekdayCode(startDayKey)) {
    return '';
  }
  if (match[1] === '-1') {
    return inLastWeek(startDayKey) ? 'last' : '';
  }
  return Number(match[1]) === weekOfMonth(startDayKey) ? 'nth' : '';
}

function inLastWeek(dayKey) {
  const [year, month] = dayKey.split('-').map(Number);
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return dayOfMonth(dayKey) + 7 > daysInMonth;
}

function dayOfMonth(dayKey) {
  return Number(String(dayKey).slice(8, 10));
}

// ---------------------------------------------------------------------------
// Wall clocks
// ---------------------------------------------------------------------------

// The UTC milliseconds of a naive local time ("YYYY-MM-DDTHH:MM:SS") in
// `timeZone`. A time skipped by a DST change resolves past the gap.
function zoneInstant(naive, timeZone) {
  const match =
    /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(
      String(naive ?? ''),
    );
  if (!match) {
    return Number.NaN;
  }
  const [year, month, day, hour, minute, second] = match
    .slice(1)
    .map((part) => Number(part ?? 0));
  const asUtc = Date.UTC(year, month - 1, day, hour, minute, second);
  // The offset at the guessed instant, then at the instant it gives.
  const guess = asUtc - zoneOffset(asUtc, timeZone);
  return asUtc - zoneOffset(guess, timeZone);
}

// How far `timeZone`'s wall clock is ahead of UTC at an instant, in ms.
function zoneOffset(instant, timeZone) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(new Date(instant))
      .map((part) => [part.type, Number(part.value)]),
  );
  const wall = Date.UTC(
    parts.year,
    parts.month - 1,
    parts.day,
    parts.hour,
    parts.minute,
    parts.second,
  );
  return wall - Math.floor(instant / 1000) * 1000;
}

// `<prefix>_date` and `<prefix>_time` of an instant on `timeZone`'s wall clock.
function wallClockFields(prefix, instant, timeZone) {
  if (!Number.isFinite(instant)) {
    return { [`${prefix}_date`]: '', [`${prefix}_time`]: '' };
  }
  const wall = new Date(instant + zoneOffset(instant, timeZone)).toISOString();
  return {
    [`${prefix}_date`]: wall.slice(0, 10),
    [`${prefix}_time`]: wall.slice(11, 16),
  };
}

function isTime(value) {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(String(value ?? ''));
}

function positiveWhole(value) {
  const text = String(value ?? '').trim();
  const number = Number(text);
  return /^\d+$/.test(text) && number >= 1 ? number : 0;
}
