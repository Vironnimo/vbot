// Calendar view controller and pure date helpers.
//
// The calendar renders a server-owned projection: `calendar.window` returns
// expanded event occurrences plus the live cron projection for one window, and
// `cron.list` names the Cron jobs bound to events (their Agent jobs). All
// instants arrive as UTC ISO strings and are rendered in the SERVER timezone
// (reported per response) — never the browser's accidental local zone, matching
// the Cron view's convention. Grid math itself works on plain calendar day
// keys ("YYYY-MM-DD"), which are timezone-neutral.

import {
  createCalendarEvent,
  deleteCalendarEvent,
  getCalendarWindow,
  listCronJobs,
  updateCalendarEvent,
} from './api.js';
import { activeLocaleTag } from './i18n.js';

export const CALENDAR_VIEWS = ['month', 'week', 'day', 'agenda'];
const AGENDA_DAYS = 14;
const MONTH_GRID_DAYS = 42;

// ---------------------------------------------------------------------------
// Pure day-key helpers. A day key is a calendar date "YYYY-MM-DD"; weekday of a
// calendar date is absolute, so all grid math runs on Date.UTC values and never
// touches the browser's local zone.
// ---------------------------------------------------------------------------

export function todayKey(timeZone = 'UTC') {
  return dayKeyInZone(new Date().toISOString(), timeZone);
}

export function addDaysToKey(key, days) {
  const [year, month, day] = key.split('-').map(Number);
  const next = new Date(
    Date.UTC(year, month - 1, day) + days * 24 * 60 * 60 * 1000,
  );
  return next.toISOString().slice(0, 10);
}

export function dayKeyToUtcDate(key) {
  return new Date(`${key}T00:00:00Z`);
}

// A real calendar date in day-key form (rejects "2026-02-30").
export function isDayKey(value) {
  return (
    typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    addDaysToKey(value, 0) === value
  );
}

// Monday-first weekday index (0..6) of a calendar day key.
function weekdayIndex(key) {
  const sundayFirst = dayKeyToUtcDate(key).getUTCDay();
  return (sundayFirst + 6) % 7;
}

export function weekStartKey(key) {
  return addDaysToKey(key, -weekdayIndex(key));
}

function monthKeyOf(key) {
  return key.slice(0, 7);
}

// The first calendar day of the six-week grid covering a month: the Monday on
// or before the 1st. Both the rendered grid and the request window derive from
// this, so the cells shown and the occurrences fetched can never disagree.
function monthGridStartKey(anchorKey) {
  const anchor = dayKeyToUtcDate(anchorKey);
  const firstOfMonth = `${anchor.getUTCFullYear()}-${pad(anchor.getUTCMonth() + 1)}-01`;
  return weekStartKey(firstOfMonth);
}

export function monthGridDays(anchorKey, currentDayKey = todayKey()) {
  const anchor = dayKeyToUtcDate(anchorKey);
  const firstOfMonth = `${anchor.getUTCFullYear()}-${pad(anchor.getUTCMonth() + 1)}-01`;
  const gridStart = dayKeyToUtcDate(monthGridStartKey(anchorKey));
  const days = [];
  for (let index = 0; index < MONTH_GRID_DAYS; index += 1) {
    const day = new Date(gridStart.getTime() + index * 24 * 60 * 60 * 1000);
    const key = day.toISOString().slice(0, 10);
    days.push({
      key,
      dayOfMonth: day.getUTCDate(),
      inMonth: monthKeyOf(key) === monthKeyOf(firstOfMonth),
      isToday: key === currentDayKey,
    });
  }
  return days;
}

export function monthLabel(year, monthIndex, locale = activeLocaleTag()) {
  return new Intl.DateTimeFormat(locale, {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, monthIndex, 1)));
}

export function dayHeadingLabel(key, locale = activeLocaleTag()) {
  return new Intl.DateTimeFormat(locale, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(dayKeyToUtcDate(key));
}

// Toolbar heading of the week view: the Monday-to-Sunday range with its year
// ("Sep 28 – Oct 4, 2026"), so compact column headers stay unambiguous when a
// week crosses a month or year boundary.
export function weekRangeLabel(anchorKey, locale = activeLocaleTag()) {
  const startKey = weekStartKey(anchorKey);
  const formatter = new Intl.DateTimeFormat(locale, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  });
  return formatter.formatRange(
    dayKeyToUtcDate(startKey),
    dayKeyToUtcDate(addDaysToKey(startKey, 6)),
  );
}

// Compact week-column header parts ("Mon" + 21). The full date remains the
// heading's accessible name and the toolbar carries month and year.
export function weekColumnLabel(key, locale = activeLocaleTag()) {
  const date = dayKeyToUtcDate(key);
  return {
    weekday: new Intl.DateTimeFormat(locale, {
      weekday: 'short',
      timeZone: 'UTC',
    }).format(date),
    dayOfMonth: date.getUTCDate(),
  };
}

export function weekdayLabels(locale = activeLocaleTag()) {
  const formatter = new Intl.DateTimeFormat(locale, {
    weekday: 'short',
    timeZone: 'UTC',
  });
  // 2023-01-02 is a Monday.
  return Array.from({ length: 7 }, (_, index) =>
    formatter.format(new Date(Date.UTC(2023, 0, 2 + index))),
  );
}

// ---------------------------------------------------------------------------
// Server-timezone rendering. Instants arrive as UTC ISO strings and are shown
// in the server's IANA zone.
// ---------------------------------------------------------------------------

function dayKeyInZone(instantIso, timeZone) {
  if (!instantIso) {
    return '';
  }
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date(instantIso));
  // en formats as "MM/DD/YYYY" or "YYYY-MM-DD" depending on engine; parse robustly.
  const match = parts.match(/(\d{4})-(\d{2})-(\d{2})/);
  if (match) {
    return `${match[1]}-${match[2]}-${match[3]}`;
  }
  const slash = parts.match(/(\d{2})\/(\d{2})\/(\d{4})/);
  if (slash) {
    return `${slash[3]}-${slash[1]}-${slash[2]}`;
  }
  return '';
}

export function formatTimeInZone(
  instantIso,
  timeZone,
  locale = activeLocaleTag(),
) {
  return new Intl.DateTimeFormat(locale, {
    timeZone,
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(instantIso));
}

// ---------------------------------------------------------------------------
// Window computation per view. Bounds are inclusive calendar day keys; the
// server expands a date bound to that full local day.
// ---------------------------------------------------------------------------

export function windowForView(view, anchorKey) {
  if (view === 'month') {
    const gridStart = monthGridStartKey(anchorKey);
    return {
      from: gridStart,
      to: addDaysToKey(gridStart, MONTH_GRID_DAYS - 1),
    };
  }
  if (view === 'week') {
    const start = weekStartKey(anchorKey);
    return { from: start, to: addDaysToKey(start, 6) };
  }
  if (view === 'day') {
    return { from: anchorKey, to: anchorKey };
  }
  return { from: anchorKey, to: addDaysToKey(anchorKey, AGENDA_DAYS - 1) };
}

// The anchor of the previous (-1) or next (+1) period of a view.
export function stepAnchor(view, anchorKey, direction) {
  if (view === 'month') {
    const anchor = dayKeyToUtcDate(anchorKey);
    const next = new Date(
      Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + direction, 1),
    );
    return `${next.getUTCFullYear()}-${pad(next.getUTCMonth() + 1)}-01`;
  }
  if (view === 'week') {
    return addDaysToKey(anchorKey, direction * 7);
  }
  if (view === 'day') {
    return addDaysToKey(anchorKey, direction);
  }
  return addDaysToKey(anchorKey, direction * AGENDA_DAYS);
}

// ---------------------------------------------------------------------------
// Occurrence grouping for the grid.
// ---------------------------------------------------------------------------

export function dayKeyForOccurrence(occurrence, timeZone) {
  if (occurrence.all_day) {
    return String(occurrence.start ?? '').slice(0, 10);
  }
  return dayKeyInZone(occurrence.start_utc, timeZone);
}

export function groupByDay(items, dayKeyOf) {
  const grouped = {};
  for (const item of items) {
    const key = dayKeyOf(item);
    if (!key) {
      continue;
    }
    if (!grouped[key]) {
      grouped[key] = [];
    }
    grouped[key].push(item);
  }
  return grouped;
}

export function sortDayEntries(entries) {
  return [...entries].sort((left, right) => {
    if (left.all_day !== right.all_day) {
      return left.all_day ? -1 : 1;
    }
    if (left.kind !== right.kind) {
      return left.kind === 'cron' ? -1 : 1;
    }
    if (left.all_day) {
      return String(left.title).localeCompare(String(right.title));
    }
    return String(left.start_utc).localeCompare(String(right.start_utc));
  });
}

export function eventById(events, eventId) {
  return events.find((event) => event.id === eventId) ?? null;
}

// The Agent jobs of one event, in created order.
export function eventJobs(jobs, eventId) {
  return jobs.filter((job) => job.event_id === eventId);
}

// When an Agent job is due for one occurrence: its start or end, shifted by
// the job's offset. Null when the occurrence has no such instant.
export function eventJobDueAt(job, occurrence) {
  const edge =
    job.event_edge === 'end' ? occurrence.end_utc : occurrence.start_utc;
  const instant = Date.parse(edge ?? '');
  if (Number.isNaN(instant)) {
    return null;
  }
  return new Date(instant + (job.event_offset_minutes ?? 0) * 60000);
}

// ---------------------------------------------------------------------------
// Controller: owns the server projection and layer toggles.
// ---------------------------------------------------------------------------

export function createCalendarViewState() {
  return {
    loading: false,
    loadError: '',
    view: 'month',
    anchorKey: todayKey(),
    occurrences: [],
    events: [],
    // Schedule Runs of the window, without those of Agent jobs, which their
    // events show.
    cron: [],
    // The Cron jobs bound to events (`schedule_type` event), as `cron.list`
    // returns them; `jobsError` says why they could not be read.
    jobs: [],
    jobsError: '',
    systemTimeZone: 'UTC',
    timeZoneResolved: false,
    showLocalLayer: true,
    showCronLayer: true,
  };
}

export function createCalendarController({ state }) {
  let loadRequestId = 0;
  let started = false;
  // The anchor only stands for "today", guessed in UTC until the first load
  // reports the server timezone.
  let followToday = false;

  // Shows one period and loads its window. `today` marks an anchor that means
  // the current day, so the first load moves it to the server's day.
  function show(view, anchorKey, { today = false } = {}) {
    followToday = today && !state.timeZoneResolved;
    if (started && state.view === view && state.anchorKey === anchorKey) {
      return Promise.resolve();
    }
    const first = !started;
    started = true;
    state.view = view;
    state.anchorKey = anchorKey;
    return load({ silent: !first });
  }

  async function load({ silent = false } = {}) {
    if (!silent) {
      state.loading = true;
    }
    state.loadError = '';
    const requestId = ++loadRequestId;
    const { from, to } = windowForView(state.view, state.anchorKey);
    // The events still show when their jobs cannot be read.
    const jobsRequest = listCronJobs().then(
      (result) => ({ jobs: result?.jobs ?? [] }),
      (error) => ({ error }),
    );
    try {
      const result = await getCalendarWindow({ from, to });
      const jobs = await jobsRequest;
      if (requestId !== loadRequestId) {
        return;
      }
      const systemTimeZone = result.system_timezone ?? 'UTC';
      const initialZoneResolution = !state.timeZoneResolved;
      state.systemTimeZone = systemTimeZone;
      state.timeZoneResolved = true;
      if (initialZoneResolution && followToday) {
        followToday = false;
        const serverToday = todayKey(systemTimeZone);
        if (serverToday !== state.anchorKey) {
          state.anchorKey = serverToday;
          return load({ silent: true });
        }
      }
      state.occurrences = result.occurrences ?? [];
      state.events = result.events ?? [];
      state.cron = (result.cron ?? []).filter((item) => !item.event_id);
      if (jobs.error) {
        state.jobsError = jobs.error?.message ?? String(jobs.error);
      } else {
        state.jobs = jobs.jobs.filter((job) => job.schedule_type === 'event');
        state.jobsError = '';
      }
    } catch (error) {
      if (requestId !== loadRequestId) {
        return;
      }
      state.loadError = error?.message ?? String(error);
    } finally {
      if (requestId === loadRequestId) {
        state.loading = false;
      }
    }
  }

  function toggleLayer(layer) {
    if (layer === 'local') {
      state.showLocalLayer = !state.showLocalLayer;
    } else if (layer === 'cron') {
      state.showCronLayer = !state.showCronLayer;
    }
  }

  async function createEvent(payload) {
    const result = await createCalendarEvent(payload);
    await load({ silent: true });
    return result;
  }

  // `id` names a whole event or, for a repeating event, one occurrence.
  async function updateEvent(id, payload) {
    const result = await updateCalendarEvent({ id, ...payload });
    await load({ silent: true });
    return result;
  }

  // A whole event goes with its Agent jobs; an occurrence id removes only
  // that occurrence.
  async function deleteEvent(id) {
    const result = await deleteCalendarEvent(id);
    await load({ silent: true });
    return result;
  }

  return {
    show,
    load,
    toggleLayer,
    createEvent,
    updateEvent,
    deleteEvent,
  };
}

function pad(value) {
  return String(value).padStart(2, '0');
}
