// The Overview's activity calendar and personal figures, from the report's
// `activity` section. Day keys are local ISO dates (`2026-10-20`) of the
// report's time zone; this module computes with them as UTC calendar dates,
// so it never depends on the browser's zone.

const DAY_MS = 86_400_000;

/** The metrics the activity calendar can color its days by. */
export const ACTIVITY_METRICS = Object.freeze(['runs', 'tokens', 'cost']);

/** The weeks the activity calendar shows: a year, ending with this week. */
export const ACTIVITY_WEEKS = 53;

// Levels above zero: a day's value is placed among the quartiles of the
// shown year's non-zero values.
const ACTIVITY_LEVELS = 4;

function dayNumber(dateKey) {
  if (typeof dateKey !== 'string') return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateKey);
  if (!match) return null;
  return Date.UTC(+match[1], +match[2] - 1, +match[3]) / DAY_MS;
}

function dateKey(number) {
  return new Date(number * DAY_MS).toISOString().slice(0, 10);
}

/** The value a day contributes to `metric`; 0 for a missing figure. */
export function activityValue(day, metric = 'runs') {
  const value =
    metric === 'tokens'
      ? day?.tokens
      : metric === 'cost'
        ? day?.cost_usd
        : day?.your_runs;
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : 0;
}

/**
 * The first day of the week in `locale`, 1 (Monday) to 7 (Sunday), from
 * the locale's week data where the browser has it; Monday otherwise.
 */
export function firstWeekday(locale = 'en') {
  try {
    const info = new Intl.Locale(locale);
    const week = info.getWeekInfo?.() ?? info.weekInfo;
    const first = week?.firstDay;
    if (Number.isInteger(first) && first >= 1 && first <= 7) return first;
  } catch {
    // An unknown locale falls back to Monday.
  }
  return 1;
}

/** Ascending thresholds that separate the non-zero levels of `values`. */
function levelThresholds(values) {
  const positive = values.filter((value) => value > 0).sort((a, b) => a - b);
  if (positive.length === 0) return [];
  return Array.from({ length: ACTIVITY_LEVELS - 1 }, (_, index) => {
    const rank = Math.ceil(((index + 1) / ACTIVITY_LEVELS) * positive.length);
    return positive[Math.max(0, rank - 1)];
  });
}

function levelOf(value, thresholds) {
  if (value <= 0) return 0;
  let level = 1;
  for (const threshold of thresholds) {
    if (value > threshold) level += 1;
  }
  return Math.min(level, ACTIVITY_LEVELS);
}

/**
 * The calendar grid of the year ending with the week of `today`: `weeks`
 * columns of seven cells starting on `weekStart` (1 Monday ... 7 Sunday).
 * Each cell is `{ date, day, value, level }` (`day` is the section's day
 * record or null, `level` 0-4), or null after `today`. `months` names the
 * first column of each month that begins inside the grid.
 */
export function activityCalendar(
  days,
  { metric = 'runs', today, weekStart = 1, weeks = ACTIVITY_WEEKS } = {},
) {
  const last = dayNumber(today);
  if (last === null) return { weeks: [], months: [] };
  const byDate = new Map();
  for (const day of days ?? []) {
    if (typeof day?.date === 'string') byDate.set(day.date, day);
  }
  // Day numbers count from Thursday, 1970-01-01 (ISO weekday 4).
  const weekday = (number) => ((((number + 3) % 7) + 7) % 7) + 1;
  const offset = (weekday(last) - weekStart + 7) % 7;
  const first = last - offset - (weeks - 1) * 7;
  const cells = [];
  for (let number = first; number <= last; number += 1) {
    const key = dateKey(number);
    const day = byDate.get(key) ?? null;
    cells.push({ date: key, day, value: activityValue(day, metric) });
  }
  const thresholds = levelThresholds(cells.map((cell) => cell.value));
  const columns = [];
  const months = [];
  for (let column = 0; column < weeks; column += 1) {
    const week = [];
    for (let row = 0; row < 7; row += 1) {
      const cell = cells[column * 7 + row];
      week.push(
        cell ? { ...cell, level: levelOf(cell.value, thresholds) } : null,
      );
      if (cell && cell.date.endsWith('-01')) {
        months.push({ column, date: cell.date });
      }
    }
    columns.push(week);
  }
  return { weeks: columns, months };
}

/** The index of the largest count (the earliest on a tie); null when all
 *  are zero. */
export function peakIndex(counts) {
  let best = null;
  (counts ?? []).forEach((count, index) => {
    if (count > 0 && (best === null || count > counts[best])) best = index;
  });
  return best;
}

/** Days from `fromKey` to `toKey`, counting both; null for a bad key. */
export function daysBetween(fromKey, toKey) {
  const from = dayNumber(fromKey);
  const to = dayNumber(toKey);
  if (from === null || to === null) return null;
  return to - from + 1;
}

/** A local date key as a UTC Date at its midnight, for date formatting. */
export function dateFromKey(key) {
  const number = dayNumber(key);
  return number === null ? null : new Date(number * DAY_MS);
}
