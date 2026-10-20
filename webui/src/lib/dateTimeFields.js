// Value helpers for the DateField and TimeField primitives.
//
// Their values are wall-clock strings without a time zone: a date is
// "YYYY-MM-DD" and a time "HH:MM" (24-hour). What the fields show and accept
// as typed text follows the app language (`activeLocaleTag()`), never the
// browser's locale. Every date computation runs on Date.UTC values, so the
// browser's own zone never shifts a day.

import { activeLocaleTag } from './i18n.js';

const DATE_VALUE = /^(\d{4})-(\d{2})-(\d{2})$/;
const TIME_VALUE = /^(\d{2}):(\d{2})$/;
const DAY_MS = 24 * 60 * 60 * 1000;
const GRID_DAYS = 42;

const formatters = new Map();

function formatter(locale, options) {
  const key = JSON.stringify([locale, options]);
  let cached = formatters.get(key);
  if (!cached) {
    cached = new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' });
    formatters.set(key, cached);
  }
  return cached;
}

const pad = (number) => String(number).padStart(2, '0');

function daysInMonth(year, month) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function toKey(year, month, day) {
  return `${String(year).padStart(4, '0')}-${pad(month)}-${pad(day)}`;
}

function utcDate(value) {
  const { year, month, day } = dateParts(value);
  return new Date(Date.UTC(year, month - 1, day));
}

// --- Dates -----------------------------------------------------------------

/** A real calendar date "YYYY-MM-DD" (rejects "2026-02-30"). */
export function isDateValue(value) {
  return dateParts(value) !== null;
}

/** { year, month (1-12), day } of a date value, or null. */
export function dateParts(value) {
  const match = typeof value === 'string' ? DATE_VALUE.exec(value) : null;
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number);
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) {
    return null;
  }
  return { year, month, day };
}

export function addDays(value, days) {
  const next = new Date(utcDate(value).getTime() + days * DAY_MS);
  return toKey(
    next.getUTCFullYear(),
    next.getUTCMonth() + 1,
    next.getUTCDate(),
  );
}

/** Moves by whole months and keeps the day where the month allows it. */
export function addMonths(value, months) {
  const { year, month, day } = dateParts(value);
  const index = year * 12 + (month - 1) + months;
  const nextYear = Math.floor(index / 12);
  const nextMonth = index - nextYear * 12 + 1;
  return toKey(
    nextYear,
    nextMonth,
    Math.min(day, daysInMonth(nextYear, nextMonth)),
  );
}

/** Whether `value` lies within the optional inclusive bounds. */
export function dateInRange(value, { min = '', max = '' } = {}) {
  return (!min || value >= min) && (!max || value <= max);
}

/** The date as a field shows it: "Wed, Sep 23, 2026". */
export function formatDateValue(value, locale = activeLocaleTag()) {
  if (!isDateValue(value)) return '';
  return formatter(locale, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  }).format(utcDate(value));
}

/** The full date as a picker day's accessible name. */
export function formatDateLong(value, locale = activeLocaleTag()) {
  if (!isDateValue(value)) return '';
  return formatter(locale, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  }).format(utcDate(value));
}

/** The picker's month heading: "September 2026". */
export function formatMonthTitle(value, locale = activeLocaleTag()) {
  if (!isDateValue(value)) return '';
  return formatter(locale, { month: 'long', year: 'numeric' }).format(
    utcDate(value),
  );
}

/** Short weekday names, Monday first like the Calendar. */
export function weekdayNames(locale = activeLocaleTag()) {
  const format = formatter(locale, { weekday: 'short' });
  // 2023-01-02 is a Monday.
  return Array.from({ length: 7 }, (_, index) =>
    format.format(new Date(Date.UTC(2023, 0, 2 + index))),
  );
}

/**
 * The six Monday-first weeks covering the month of `value`, as 42 date
 * values; each carries whether it belongs to that month.
 */
export function monthGrid(value) {
  const { year, month } = dateParts(value);
  const first = toKey(year, month, 1);
  const mondayOffset = (utcDate(first).getUTCDay() + 6) % 7;
  const start = addDays(first, -mondayOffset);
  return Array.from({ length: GRID_DAYS }, (_, index) => {
    const key = addDays(start, index);
    return {
      value: key,
      day: Number(key.slice(8)),
      inMonth: key.slice(0, 7) === first.slice(0, 7),
    };
  });
}

// Order of day, month and year in the language's numeric dates ("9/23/2026"
// reads month first in English).
function numericOrder(locale) {
  return formatter(locale, {
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  })
    .formatToParts(new Date(Date.UTC(2026, 8, 23)))
    .map((part) => part.type)
    .filter((type) => type === 'year' || type === 'month' || type === 'day');
}

function monthNames(locale) {
  const names = [];
  for (const style of ['long', 'short']) {
    const format = formatter(locale, { month: style });
    for (let month = 1; month <= 12; month += 1) {
      names.push({
        name: normalizeWord(format.format(new Date(Date.UTC(2026, month - 1)))),
        month,
      });
    }
  }
  return names;
}

function normalizeWord(word) {
  return word.toLocaleLowerCase().normalize('NFD').replace(/[̀-ͯ.]/g, '');
}

function isWeekdayWord(word, locale) {
  const normalized = normalizeWord(word);
  if (normalized.length < 2) return false;
  return ['long', 'short'].some((style) => {
    const format = formatter(locale, { weekday: style });
    return Array.from({ length: 7 }, (_, index) =>
      normalizeWord(format.format(new Date(Date.UTC(2023, 0, 2 + index)))),
    ).some((name) => name.startsWith(normalized));
  });
}

function monthFromWord(word, locale) {
  const normalized = normalizeWord(word);
  if (normalized.length < 3) return null;
  const found = monthNames(locale).find(({ name }) =>
    name.startsWith(normalized),
  );
  return found ? found.month : null;
}

function fullYear(text) {
  const year = Number(text);
  return text.length <= 2 ? 2000 + year : year;
}

function validDate(year, month, day) {
  if (
    !Number.isInteger(year) ||
    year < 1 ||
    year > 9999 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysInMonth(year, month)
  ) {
    return null;
  }
  return toKey(year, month, day);
}

/**
 * Reads a typed date. Accepts the field's own display ("Wed, Sep 23, 2026"),
 * month names ("23 September", "sep 23"), numeric dates in the language's
 * order ("9/23/2026", "9/23") and ISO dates ("2026-09-23"). A missing year is
 * taken from `referenceValue` (the field's current date) or today's.
 *
 * @returns {string|null} a date value, '' for empty text, or null when the
 *   text is no date.
 */
export function parseDateText(
  text,
  { locale = activeLocaleTag(), referenceValue = '' } = {},
) {
  const trimmed = String(text ?? '').trim();
  if (!trimmed) return '';
  const iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(trimmed);
  if (iso) return validDate(Number(iso[1]), Number(iso[2]), Number(iso[3]));

  const reference = dateParts(referenceValue);
  const referenceYear = reference?.year ?? new Date().getUTCFullYear();
  const tokens = trimmed.split(/[\s,./-]+/u).filter(Boolean);
  const numbers = tokens.filter((token) => /^\d+$/.test(token));
  const words = tokens.filter((token) => !/^\d+$/.test(token));

  // Besides one month name, only weekday names ("Wed") may appear.
  let month = null;
  for (const word of words) {
    const named = monthFromWord(word, locale);
    if (named !== null && month === null) {
      month = named;
    } else if (!isWeekdayWord(word, locale)) {
      return null;
    }
  }

  if (month !== null) {
    // "23 September 2026" or "Sep 23, 2026": the day is the number up to 31
    // that does not look like a year.
    if (numbers.length === 0 || numbers.length > 2) return null;
    const [first, second] = numbers;
    if (numbers.length === 1) {
      return validDate(referenceYear, month, Number(first));
    }
    const firstIsYear = first.length > 2 || Number(first) > 31;
    const day = Number(firstIsYear ? second : first);
    const year = fullYear(firstIsYear ? first : second);
    return validDate(year, month, day);
  }

  if (numbers.length < 2 || numbers.length > 3) return null;
  if (numbers[0].length === 4) {
    if (numbers.length !== 3) return null;
    return validDate(
      Number(numbers[0]),
      Number(numbers[1]),
      Number(numbers[2]),
    );
  }
  const order = numericOrder(locale);
  const parts = {};
  const fields =
    numbers.length === 3 ? order : order.filter((type) => type !== 'year');
  fields.forEach((type, index) => {
    parts[type] = numbers[index];
  });
  return validDate(
    parts.year ? fullYear(parts.year) : referenceYear,
    Number(parts.month),
    Number(parts.day),
  );
}

// --- Times -----------------------------------------------------------------

/** { hour (0-23), minute } of a time value, or null. */
export function timeParts(value) {
  const match = typeof value === 'string' ? TIME_VALUE.exec(value) : null;
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return null;
  return { hour, minute };
}

export function isTimeValue(value) {
  return timeParts(value) !== null;
}

/** Whether the language writes times with AM/PM. */
export function uses12HourClock(locale = activeLocaleTag()) {
  const cycle = formatter(locale, { hour: 'numeric' }).resolvedOptions()
    .hourCycle;
  return cycle === 'h12' || cycle === 'h11';
}

/** The time as a field shows it: "9:30 AM", or "09:30" in a 24-hour language. */
export function formatTimeValue(value, locale = activeLocaleTag()) {
  const parts = timeParts(value);
  if (!parts) return '';
  return formatter(locale, {
    hour: uses12HourClock(locale) ? 'numeric' : '2-digit',
    minute: '2-digit',
  }).format(new Date(Date.UTC(2000, 0, 1, parts.hour, parts.minute)));
}

function dayPeriods(locale) {
  const format = formatter(locale, { hour: 'numeric', hour12: true });
  const period = (hour) =>
    normalizeWord(
      format
        .formatToParts(new Date(Date.UTC(2000, 0, 1, hour)))
        .find((part) => part.type === 'dayPeriod')?.value ?? '',
    ).replace(/\s/g, '');
  return { am: period(9), pm: period(21) };
}

function readMeridiem(text, locale) {
  const compact = normalizeWord(text).replace(/\s/g, '');
  const periods = dayPeriods(locale);
  const candidates = [
    ['pm', periods.pm],
    ['am', periods.am],
    ['pm', 'pm'],
    ['am', 'am'],
    ['pm', 'p'],
    ['am', 'a'],
  ];
  for (const [meridiem, word] of candidates) {
    if (word && compact.endsWith(word)) {
      return {
        meridiem,
        rest: compact.slice(0, compact.length - word.length),
      };
    }
  }
  return { meridiem: '', rest: compact };
}

/**
 * Reads a typed time: "9", "930", "0930", "9:30", "21:30", "9.30", "9:30 pm",
 * "9p", "12am". Without AM/PM an hour reads as on a 24-hour clock.
 *
 * @returns {string|null} a time value, '' for empty text, or null when the
 *   text is no time.
 */
export function parseTimeText(text, { locale = activeLocaleTag() } = {}) {
  const trimmed = String(text ?? '').trim();
  if (!trimmed) return '';
  const { meridiem, rest } = readMeridiem(trimmed, locale);
  let hourText;
  let minuteText = '00';
  const separated = /^(\d{1,2})[:.h](\d{2})$/.exec(rest);
  if (separated) {
    [, hourText, minuteText] = separated;
  } else if (/^\d{1,2}$/.test(rest)) {
    hourText = rest;
  } else if (/^\d{3,4}$/.test(rest)) {
    hourText = rest.slice(0, rest.length - 2);
    minuteText = rest.slice(-2);
  } else {
    return null;
  }
  let hour = Number(hourText);
  const minute = Number(minuteText);
  if (minute > 59) return null;
  if (meridiem) {
    if (hour < 1 || hour > 12) return null;
    hour = (hour % 12) + (meridiem === 'pm' ? 12 : 0);
  } else if (hour > 23) {
    return null;
  }
  return `${pad(hour)}:${pad(minute)}`;
}

/** Every time of a day in steps of `step` minutes. */
export function timeOptions(step = 30) {
  const minutes = Math.min(Math.max(Math.round(step), 5), 60 * 12);
  const options = [];
  for (let offset = 0; offset < 24 * 60; offset += minutes) {
    options.push(`${pad(Math.floor(offset / 60))}:${pad(offset % 60)}`);
  }
  return options;
}

/** The option a value falls on, or the last one before it. */
export function nearestTimeOption(options, value) {
  if (!isTimeValue(value)) return '';
  let found = options[0] ?? '';
  for (const option of options) {
    if (option <= value) found = option;
  }
  return found;
}
