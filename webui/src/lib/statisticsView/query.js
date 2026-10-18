import {
  dateTimePartsInZone,
  timeZoneOffsetMilliseconds,
} from '../dateTimePrefs.svelte.js';
import { t } from '../i18n.js';

const DAY_MS = 86_400_000;

export const STATISTICS_TABS = Object.freeze([
  'overview',
  'usage',
  'runs',
  'tools',
  'limits',
  'extensions',
  'diagnostics',
]);

// The `statistics.report` sections one tab renders, requested in one call.
// Limits reads live provider usage instead of the report.
const TAB_SECTIONS = Object.freeze({
  overview: Object.freeze(['overview']),
  usage: Object.freeze(['usage']),
  runs: Object.freeze(['runs']),
  tools: Object.freeze(['tools', 'skills']),
  extensions: Object.freeze(['extensions']),
  diagnostics: Object.freeze(['diagnostics']),
});

// Places of retired tabs open the tab that now holds their content.
const RETIRED_PLACES = Object.freeze({
  compactions: 'diagnostics',
  skills: 'tools',
});

export const STATISTICS_RANGES = Object.freeze([
  '24h',
  '7d',
  '30d',
  '90d',
  'all',
]);
export const DEFAULT_STATISTICS_RANGE = '30d';
const RANGE_DAYS = Object.freeze({ '7d': 7, '30d': 30, '90d': 90 });

/** The report sections a tab requests; none for Limits. */
export function statisticsTabSections(tab) {
  return Object.hasOwn(TAB_SECTIONS, tab) ? TAB_SECTIONS[tab] : [];
}

/** The tab a navigation place shows: a tab id, a retired tab's successor,
 *  or the Overview. */
export function statisticsTabForPlace(place) {
  if (STATISTICS_TABS.includes(place)) return place;
  return Object.hasOwn(RETIRED_PLACES, place)
    ? RETIRED_PLACES[place]
    : STATISTICS_TABS[0];
}

const TAB_LABELS = Object.freeze({
  overview: () => t('statistics.subview.overview'),
  usage: () => t('statistics.subview.usage'),
  runs: () => t('statistics.subview.runs'),
  tools: () => t('statistics.subview.tools'),
  limits: () => t('statistics.subview.limits'),
  extensions: () => t('statistics.subview.extensions'),
  diagnostics: () => t('statistics.subview.diagnostics'),
});

/** A tab's name. */
export function statisticsTabLabel(tab) {
  return (TAB_LABELS[tab] ?? TAB_LABELS.overview)();
}

export function isStatisticsRange(value) {
  return STATISTICS_RANGES.includes(value);
}

function zonedParts(instant, timeZone) {
  try {
    return dateTimePartsInZone(instant, timeZone || 'UTC');
  } catch {
    return dateTimePartsInZone(instant, 'UTC');
  }
}

// Statistics deliberately treats an unsupported zone as UTC for window
// projection; the original requested zone is still sent to the server.
function zoneOffset(instant, timeZone) {
  try {
    return timeZoneOffsetMilliseconds(instant, timeZone || 'UTC');
  } catch {
    return timeZoneOffsetMilliseconds(instant, 'UTC');
  }
}

// The instant of local midnight on a calendar day in `timeZone`; the second
// pass corrects a day whose offset differs from midnight UTC's (DST).
function zonedMidnight(year, monthIndex, day, timeZone) {
  const wall = Date.UTC(year, monthIndex, day);
  const first = wall - zoneOffset(wall, timeZone);
  return wall - zoneOffset(first, timeZone);
}

/**
 * The report window of a range: `24h` is the rolling last day, `7d`, `30d`
 * and `90d` are that many calendar days in `timeZone` including today, and
 * `all` has no start. The server floors `since` to the hour.
 */
export function statisticsWindow(range, timeZone = 'UTC', now = Date.now()) {
  if (range === '24h') {
    return { since: new Date(now - DAY_MS).toISOString() };
  }
  const days = RANGE_DAYS[range];
  if (!days) return {};
  const today = zonedParts(now, timeZone);
  const start = zonedMidnight(
    today.year,
    today.month - 1,
    today.day - (days - 1),
    timeZone,
  );
  return { since: new Date(start).toISOString() };
}

/** One report's cache identity and request window. The window is resolved
 * at request time, so a retained query never freezes a rolling range. */
export class StatisticsReportQuery {
  constructor(tab, range, timeZone = 'UTC') {
    this.tab = tab;
    this.range = range;
    this.timeZone = timeZone;
    Object.freeze(this);
  }

  get key() {
    return `${statisticsTabSections(this.tab).join('+')}|${this.range}|${this.timeZone}`;
  }

  params(now = Date.now()) {
    return {
      ...statisticsWindow(this.range, this.timeZone, now),
      timezone: this.timeZone,
      sections: [...statisticsTabSections(this.tab)],
    };
  }
}

/** The `statistics.report` parameters of one tab, range and time zone. */
export function statisticsReportParams(
  tab,
  range,
  timeZone = 'UTC',
  now = Date.now(),
) {
  return new StatisticsReportQuery(tab, range, timeZone).params(now);
}

/** The cache key of one tab's report: its sections, range and time zone. */
export function statisticsReportKey(tab, range, timeZone = 'UTC') {
  return new StatisticsReportQuery(tab, range, timeZone).key;
}
