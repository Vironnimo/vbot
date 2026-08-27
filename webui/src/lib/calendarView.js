// Calendar view controller and pure date helpers.
//
// The calendar renders a server-owned projection: `calendar.window` returns
// expanded event occurrences plus the live cron projection for one window. All
// instants arrive as UTC ISO strings and are rendered in the SERVER timezone
// (reported per response) — never the browser's accidental local zone, matching
// the Cron view's convention. Grid math itself works on plain calendar day
// keys ("YYYY-MM-DD"), which are timezone-neutral.

import {
  createCalendarEvent,
  deleteCalendarEvent,
  getCalendarWindow,
  updateCalendarEvent,
} from './api.js';
import { activeLocaleTag } from './i18n.js';

export const CALENDAR_VIEWS = ['month', 'week', 'day', 'agenda'];
const AGENDA_DAYS = 14;

// ---------------------------------------------------------------------------
// Pure day-key helpers. A day key is a calendar date "YYYY-MM-DD"; weekday of a
// calendar date is absolute, so all grid math runs on Date.UTC values and never
// touches the browser's local zone.
// ---------------------------------------------------------------------------

export function todayKey() {
  const now = new Date();
  return `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${now.getUTCDate()}`;
}

export function isDayKey(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
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

// Monday-first weekday index (0..6) of a calendar day key.
export function weekdayIndex(key) {
  const sundayFirst = dayKeyToUtcDate(key).getUTCDay();
  return (sundayFirst + 6) % 7;
}

export function weekStartKey(key) {
  return addDaysToKey(key, -weekdayIndex(key));
}

export function monthKeyOf(key) {
  return key.slice(0, 7);
}

export function monthGridDays(anchorKey) {
  const anchor = dayKeyToUtcDate(anchorKey);
  const year = anchor.getUTCFullYear();
  const month = anchor.getUTCMonth();
  const firstOfMonth = `${year}-${pad(month + 1)}-01`;
  const gridStart = dayKeyToUtcDate(weekStartKey(firstOfMonth));
  const days = [];
  for (let index = 0; index < 42; index += 1) {
    const day = new Date(gridStart.getTime() + index * 24 * 60 * 60 * 1000);
    const key = day.toISOString().slice(0, 10);
    days.push({
      key,
      dayOfMonth: day.getUTCDate(),
      inMonth: monthKeyOf(key) === monthKeyOf(firstOfMonth),
      isToday: key === todayKey(),
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

export function dayKeyInZone(instantIso, timeZone) {
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

export function formatOccurrenceTime(
  occurrence,
  timeZone,
  locale = activeLocaleTag(),
) {
  if (occurrence.all_day) {
    return null;
  }
  return formatTimeInZone(occurrence.start_utc, timeZone, locale);
}

// ---------------------------------------------------------------------------
// Window computation per view. Bounds are inclusive calendar day keys; the
// server expands a date bound to that full local day.
// ---------------------------------------------------------------------------

export function windowForView(view, anchorKey) {
  if (view === 'month') {
    const anchor = dayKeyToUtcDate(anchorKey);
    const first = `${anchor.getUTCFullYear()}-${pad(anchor.getUTCMonth() + 1)}-01`;
    const lastDay = new Date(
      Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth() + 1, 0),
    );
    return { from: first, to: lastDay.toISOString().slice(0, 10) };
  }
  if (view === 'week') {
    const start = weekStartKey(anchorKey);
    return { from: start, to: addDaysToKey(start, 6) };
  }
  if (view === 'day') {
    return { from: anchorKey, to: anchorKey };
  }
  const today = todayKey();
  return { from: today, to: addDaysToKey(today, AGENDA_DAYS - 1) };
}

export function navigateAnchor(view, anchorKey, direction) {
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
    return occurrence.start_date;
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

// Build the editable form payload from one event record.
export function eventToFormValues(event) {
  const rrule = event.rrule ?? null;
  return {
    title: event.title ?? '',
    notes: event.notes ?? '',
    all_day: Boolean(event.all_day),
    start_date: event.all_day
      ? event.start_date
      : dayKeyFromInstantOrLocal(event),
    start_time: event.all_day
      ? '09:00'
      : (event.start_local ?? '').slice(11, 16) || '09:00',
    duration_minutes: event.duration_minutes ?? 60,
    duration_days: event.duration_days ?? 1,
    freq: rrule?.freq ?? 'none',
    interval: rrule?.interval ?? 1,
    by_weekday: rrule?.by_weekday ?? ['mo', 'tu', 'we', 'th', 'fr'],
    end_mode: rrule
      ? rrule.count != null
        ? 'count'
        : rrule.until != null
          ? 'until'
          : 'never'
      : 'never',
    end_count: rrule?.count ?? 10,
    end_until: rrule?.until ?? '',
  };
}

function dayKeyFromInstantOrLocal(event) {
  if (event.start_utc) {
    return event.start_utc.slice(0, 10);
  }
  return (event.start_local ?? '').slice(0, 10);
}

// Build the create/update RPC payload from form values. Recurrence "none"
// clears the rule; weekly recurrence carries the selected weekdays.
export function formValuesToPayload(values) {
  const payload = {
    title: values.title,
    notes: values.notes || null,
    all_day: values.all_day,
  };
  if (values.all_day) {
    payload.start = values.start_date;
    payload.duration_days = Number(values.duration_days) || 1;
  } else {
    payload.start = `${values.start_date}T${values.start_time || '09:00'}:00`;
    payload.duration_minutes = Number(values.duration_minutes) || 60;
  }
  if (values.freq !== 'none') {
    const rrule = { freq: values.freq, interval: Number(values.interval) || 1 };
    if (values.freq === 'weekly') {
      rrule.by_weekday = values.by_weekday?.length ? values.by_weekday : ['mo'];
    }
    if (values.end_mode === 'count') {
      rrule.count = Number(values.end_count) || 10;
    } else if (values.end_mode === 'until') {
      rrule.until = values.end_until || undefined;
    }
    payload.rrule = rrule;
  } else {
    payload.rrule = null;
  }
  return payload;
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
    cron: [],
    systemTimeZone: 'UTC',
    showLocalLayer: true,
    showCronLayer: true,
  };
}

export function createCalendarController({ state }) {
  let loadRequestId = 0;

  async function load({ silent = false } = {}) {
    if (!silent) {
      state.loading = true;
    }
    state.loadError = '';
    const requestId = ++loadRequestId;
    const { from, to } = windowForView(state.view, state.anchorKey);
    try {
      const result = await getCalendarWindow({ from, to });
      if (requestId !== loadRequestId) {
        return;
      }
      state.occurrences = result.occurrences ?? [];
      state.events = result.events ?? [];
      state.cron = result.cron ?? [];
      state.systemTimeZone = result.system_timezone ?? 'UTC';
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

  function setView(view) {
    state.view = view;
    load({ silent: true });
  }

  function setAnchor(anchorKey) {
    state.anchorKey = anchorKey;
    load({ silent: true });
  }

  function navigate(direction) {
    state.anchorKey = navigateAnchor(state.view, state.anchorKey, direction);
    load({ silent: true });
  }

  function goToday() {
    state.anchorKey = todayKey();
    load({ silent: true });
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

  async function updateEvent(eventId, payload) {
    const result = await updateCalendarEvent({ id: eventId, ...payload });
    await load({ silent: true });
    return result;
  }

  async function deleteEvent(eventId) {
    await deleteCalendarEvent(eventId);
    await load({ silent: true });
  }

  async function excludeOccurrence(eventId, occurrenceStart) {
    const event = state.events.find((item) => item.id === eventId);
    const exdates = [...(event?.exdates ?? []), occurrenceStart];
    await updateCalendarEvent({ id: eventId, exdates });
    await load({ silent: true });
  }

  return {
    load,
    setView,
    setAnchor,
    navigate,
    goToday,
    toggleLayer,
    createEvent,
    updateEvent,
    deleteEvent,
    excludeOccurrence,
  };
}

function pad(value) {
  return String(value).padStart(2, '0');
}
