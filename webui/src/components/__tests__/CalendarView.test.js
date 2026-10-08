// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { createStandaloneNavigation } from '../../lib/navigation.svelte.js';
import { rpcBackedApiMock } from './apiMock.support.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: CalendarView } = await import('../CalendarView.svelte');

function calendarWindow({
  events = [],
  occurrences = [],
  cron = [],
  timezone = 'UTC',
} = {}) {
  return { events, occurrences, cron, system_timezone: timezone };
}

// A stored event; timed times are naive local times of `tz_name`.
function calendarEvent(overrides = {}) {
  return {
    id: 'evt-1',
    title: 'Standup',
    description: null,
    location: null,
    all_day: false,
    recurring: false,
    start: '2026-09-23T09:00:00',
    end: '2026-09-23T09:30:00',
    tz_name: 'UTC',
    rrule: null,
    exdates: [],
    overrides: {},
    ...overrides,
  };
}

function occurrence(title, startUtc, endUtc, overrides = {}) {
  return {
    id: `evt-${title}`,
    event_id: `evt-${title}`,
    title,
    description: null,
    location: null,
    all_day: false,
    recurring: false,
    start: startUtc.slice(0, 19),
    end: endUtc.slice(0, 19),
    start_utc: startUtc,
    end_utc: endUtc,
    original_start: startUtc.slice(0, 19),
    overridden: false,
    ...overrides,
  };
}

// The weekly Standup on Wednesdays and its occurrence on 2026-09-23.
function weeklyStandup() {
  return calendarWindow({
    events: [calendarEvent({ recurring: true, rrule: 'FREQ=WEEKLY;BYDAY=WE' })],
    occurrences: [
      occurrence(
        'Standup',
        '2026-09-23T09:00:00+00:00',
        '2026-09-23T09:30:00+00:00',
        { id: 'evt-1_20260923T0900', event_id: 'evt-1', recurring: true },
      ),
    ],
  });
}

// An Agent job of `evt-1` as `cron.list` returns it.
function eventJob(overrides = {}) {
  return {
    id: 'cron-1',
    target: 'main',
    agent_id: 'main',
    name: 'Prepare the agenda',
    prompt: 'Prepare the agenda',
    schedule_type: 'event',
    event_id: 'evt-1',
    event_title: 'Standup',
    event_edge: 'start',
    event_offset_minutes: -30,
    session_id: null,
    status: 'active',
    last_outcome: null,
    next_fire_at: '2026-09-23T08:30:00+00:00',
    ...overrides,
  };
}

// The open quick tooltip's rows as [label, value] pairs.
function tooltipRows() {
  return [...document.querySelectorAll('#app-tooltip dd')].map((value) => [
    value.previousElementSibling?.tagName === 'DT'
      ? value.previousElementSibling.textContent
      : '',
    value.textContent,
  ]);
}

function focusWithKeyboard(element) {
  document.body.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }),
  );
  element.focus();
  flushSync();
}

function serveWindow(window, jobs = []) {
  rpcMock.mockImplementation((method) => {
    if (method === 'calendar.window') {
      return Promise.resolve(window);
    }
    if (method === 'cron.list') {
      return Promise.resolve({ jobs, system_timezone: window.system_timezone });
    }
    if (method === 'agent.list') {
      return Promise.resolve({ agents: [{ id: 'main', name: 'Main' }] });
    }
    if (method === 'project.list') {
      return Promise.resolve({ projects: [] });
    }
    if (method === 'session.list') {
      return Promise.resolve({ sessions: [], next_cursor: null });
    }
    if (method.startsWith('calendar.')) {
      return Promise.resolve({});
    }
    return Promise.reject(new Error(`Unexpected RPC: ${method}`));
  });
}

async function waitForCondition(predicate, attempts = 50) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    flushSync();
    if (predicate()) {
      return;
    }
    await Promise.resolve();
  }
  flushSync();
  if (!predicate()) {
    throw new Error('condition not met');
  }
}

function showView(view) {
  const label = t(`calendar.view.${view}`);
  [...document.querySelectorAll('[role="tab"]')]
    .find((tab) => tab.textContent.trim() === label)
    .click();
  flushSync();
}

// Intl separates date ranges with (thin) spaces; compare plain text.
function plainText(element) {
  return element.textContent.replace(/\s+/g, ' ').trim();
}

function button(label, root = document) {
  const match = [...root.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  expect(match, `button not found: ${label}`).toBeTruthy();
  return match;
}

function chooseOption(id, label) {
  document.getElementById(id).click();
  flushSync();
  const option = [
    ...document.querySelectorAll(`#${id}-listbox [role="option"]`),
  ].find((item) => item.textContent.trim() === label);
  expect(option).toBeTruthy();
  option.click();
  flushSync();
}

function typeInto(id, value) {
  const input = document.getElementById(id);
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function rpcCalls(method) {
  return rpcMock.mock.calls
    .filter(([name]) => name === method)
    .map(([, params]) => params);
}

async function mountCalendarView() {
  const component = mount(CalendarView, { target: document.body });
  flushSync();
  await waitForCondition(
    () => document.querySelector('.calendar-grid') !== null,
  );
  return component;
}

describe('CalendarView', () => {
  let mountedComponent = null;

  beforeEach(() => {
    init('en');
    rpcMock.mockReset();
    serveWindow(calendarWindow());
  });

  afterEach(() => {
    if (mountedComponent) {
      unmount(mountedComponent);
      mountedComponent = null;
    }
  });

  it('renders the month grid with today when nothing is scheduled', async () => {
    mountedComponent = await mountCalendarView();

    // Regression: an empty window used to replace the whole calendar with an
    // empty state; the grid itself shows that nothing is scheduled.
    expect(document.querySelectorAll('.calendar-weekday')).toHaveLength(7);
    expect(document.querySelectorAll('.calendar-cell')).toHaveLength(42);
    expect(document.querySelector('.calendar-cell.is-today')).not.toBeNull();
    expect(document.querySelector('.empty-state__title')).toBeNull();
  });

  describe('on a fixed day', () => {
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['Date'] });
      vi.setSystemTime(new Date('2026-09-23T12:00:00Z'));
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('heads week columns with compact labels under a week range', async () => {
      mountedComponent = await mountCalendarView();
      showView('week');

      // The toolbar names the week once; columns show only weekday and day
      // instead of repeating the full date and year seven times.
      expect(plainText(document.querySelector('.calendar-heading'))).toBe(
        'Sep 21 – 27, 2026',
      );
      const headings = [
        ...document.querySelectorAll('.calendar-column-heading'),
      ];
      expect(headings.map(plainText)).toEqual([
        'Mon 21',
        'Tue 22',
        'Wed 23',
        'Thu 24',
        'Fri 25',
        'Sat 26',
        'Sun 27',
      ]);
      expect(headings[0].getAttribute('aria-label')).toBe(
        'Monday, September 21, 2026',
      );
      const today = document.querySelector('.calendar-column.is-today');
      expect(plainText(today.querySelector('.calendar-column-heading'))).toBe(
        'Wed 23',
      );
    });

    it('shows the period named by the place and makes each period change a step', async () => {
      const navigation = createStandaloneNavigation(['week', '2026-09-02']);
      mountedComponent = mount(CalendarView, {
        target: document.body,
        props: { navigation },
      });
      await waitForCondition(
        () => document.querySelector('.calendar-columns') !== null,
      );
      expect(plainText(document.querySelector('.calendar-heading'))).toBe(
        'Aug 31 – Sep 6, 2026',
      );
      expect(rpcCalls('calendar.window')[0]).toEqual({
        from: '2026-08-31',
        to: '2026-09-06',
      });

      document
        .querySelector(`button[aria-label="${t('calendar.prevWeek')}"]`)
        .click();
      flushSync();
      expect(navigation.place).toEqual(['week', '2026-08-26']);
      expect(plainText(document.querySelector('.calendar-heading'))).toBe(
        'Aug 24 – 30, 2026',
      );
      showView('month');
      expect(navigation.place).toEqual(['month', '2026-08-26']);
      button(t('calendar.today')).click();
      flushSync();
      expect(navigation.place).toEqual(['month', '2026-09-23']);

      // The empty place (the view's start page) names the month around today.
      navigation.navigate([]);
      flushSync();
      expect(navigation.place).toEqual(['month', '2026-09-23']);
    });

    it('names the shown day once in the day view', async () => {
      mountedComponent = await mountCalendarView();
      showView('day');

      expect(plainText(document.querySelector('.calendar-heading'))).toBe(
        'Wednesday, September 23, 2026',
      );
      expect(document.querySelector('.calendar-column-heading')).toBeNull();
      expect(
        document.querySelector('.calendar-column').getAttribute('aria-label'),
      ).toBe('Wednesday, September 23, 2026');
    });

    it('marks days of neighbouring months in the month grid', async () => {
      mountedComponent = await mountCalendarView();

      const outside = [
        ...document.querySelectorAll('.calendar-cell.is-outside'),
      ].map((cell) => plainText(cell.querySelector('.calendar-day-number')));
      // The six-week grid of September 2026 runs from Monday, August 31 to
      // Sunday, October 11.
      expect(outside).toEqual([
        '31',
        ...Array.from({ length: 11 }, (_, index) => String(index + 1)),
      ]);
    });

    it('opens the create form from the toolbar or an empty day in a padded modal with labelled controls', async () => {
      mountedComponent = await mountCalendarView();

      button(t('calendar.newEvent')).click();
      flushSync();

      // The Modal shell renders body snippets directly; callers own the padded
      // `.modal-body` wrapper. Without it the form sits flush against the
      // modal edges.
      expect(
        document.querySelector('.modal-body .calendar-form'),
      ).not.toBeNull();
      for (const id of [
        'calendar-form-title-input',
        'calendar-form-location',
        'calendar-form-start-date',
        'calendar-form-start-time',
        'calendar-form-end-date',
        'calendar-form-end-time',
      ]) {
        expect(
          document.querySelector(`.calendar-form label[for="${id}"]`),
        ).not.toBeNull();
        expect(document.querySelector(`.calendar-form input#${id}`)).not.toBe(
          null,
        );
      }
      expect(document.getElementById('calendar-form-start-date').value).toBe(
        '2026-09-23',
      );

      button(t('common.cancel')).click();
      flushSync();
      expect(document.querySelector('.calendar-form')).toBeNull();

      // The first surface of the September grid is Monday, August 31.
      document.querySelector('.calendar-cell-surface').click();
      flushSync();
      expect(document.getElementById('calendar-form-start-date').value).toBe(
        '2026-08-31',
      );
    });

    it('creates a weekly recurring event through the shared choice fields', async () => {
      mountedComponent = await mountCalendarView();
      button(t('calendar.newEvent')).click();
      flushSync();

      // The labels still name the Dropdown triggers.
      const freqTrigger = document.getElementById('calendar-form-freq');
      expect(freqTrigger.tagName).toBe('BUTTON');
      expect(
        document.querySelector(
          '.calendar-form label[for="calendar-form-freq"]',
        ),
      ).not.toBeNull();
      expect(document.querySelector('.calendar-form select')).toBeNull();
      expect(document.querySelector('.calendar-weekday-picker')).toBeNull();

      typeInto('calendar-form-title-input', 'Standup');
      const weekly = t('calendar.form.freqWeekly');
      chooseOption('calendar-form-freq', weekly);
      expect(freqTrigger.textContent.trim()).toBe(weekly);
      expect(document.querySelector('.calendar-weekday-picker')).not.toBeNull();
      chooseOption('calendar-form-end-mode', t('calendar.form.endsUntil'));
      expect(
        document.querySelector('.calendar-form-ends input[type="date"]'),
      ).not.toBeNull();
      chooseOption('calendar-form-end-mode', t('calendar.form.endsCount'));
      expect(
        document.querySelector('.calendar-form-ends input[type="number"]'),
      ).not.toBeNull();

      button(t('calendar.form.create')).click();
      await waitForCondition(
        () => document.querySelector('.calendar-form') === null,
      );

      // A new weekly event repeats on its start's weekday, a Wednesday.
      expect(rpcCalls('calendar.create')).toEqual([
        {
          title: 'Standup',
          description: null,
          location: null,
          start: '2026-09-23T09:00:00',
          end: '2026-09-23T10:00:00',
          rrule: 'FREQ=WEEKLY;BYDAY=WE;COUNT=10',
        },
      ]);
    });

    it('enters an all-day event by its last day and stores the day after', async () => {
      mountedComponent = await mountCalendarView();
      button(t('calendar.newEvent')).click();
      flushSync();
      typeInto('calendar-form-title-input', 'Trip');
      document.querySelector('.calendar-form-toggle [role="switch"]').click();
      flushSync();

      expect(document.getElementById('calendar-form-start-time')).toBeNull();
      expect(
        document.querySelector(
          '.calendar-form label[for="calendar-form-end-date"]',
        ).textContent,
      ).toContain(t('calendar.form.lastDay'));
      typeInto('calendar-form-end-date', '2026-09-25');
      button(t('calendar.form.create')).click();
      await waitForCondition(() => rpcCalls('calendar.create').length === 1);

      expect(rpcCalls('calendar.create')[0]).toMatchObject({
        start: '2026-09-23',
        end: '2026-09-26',
        rrule: null,
      });
    });

    it('opens an entry in the detail modal and saves an edit in the server wall clock', async () => {
      serveWindow(
        calendarWindow({
          timezone: 'Europe/Berlin',
          events: [
            calendarEvent({
              title: 'Dentist',
              location: 'Main Street 4',
              start: '2026-09-23T09:00:00',
              end: '2026-09-23T10:00:00',
              tz_name: 'Europe/Berlin',
            }),
          ],
          occurrences: [
            occurrence(
              'Dentist',
              '2026-09-23T07:00:00+00:00',
              '2026-09-23T08:00:00+00:00',
              { id: 'evt-1', event_id: 'evt-1', location: 'Main Street 4' },
            ),
          ],
        }),
      );
      mountedComponent = await mountCalendarView();

      // The entry's card gives the complete title, the zone of its time and
      // its location.
      focusWithKeyboard(
        document.querySelector('.calendar-cell .calendar-entry'),
      );
      expect(
        document.querySelector('#app-tooltip .app-tooltip__title').textContent,
      ).toBe('Dentist');
      expect(tooltipRows()[1]).toEqual([
        t('calendar.details.timeZone'),
        'Europe/Berlin',
      ]);
      expect(tooltipRows()).toContainEqual([
        t('calendar.form.location'),
        'Main Street 4',
      ]);

      document.querySelector('.calendar-cell .calendar-entry').click();
      flushSync();

      // Regression: clicking an entry used to bubble to the create surface, so
      // the form opened instead of the detail modal.
      expect(document.querySelector('.calendar-detail')).not.toBeNull();
      expect(document.querySelector('.calendar-form')).toBeNull();
      expect(document.querySelector('.calendar-detail').textContent).toContain(
        'Main Street 4',
      );

      button(t('common.edit')).click();
      flushSync();
      // 07:00 UTC is 09:00 in Berlin; the form presents that wall clock.
      expect(document.getElementById('calendar-form-start-date').value).toBe(
        '2026-09-23',
      );
      expect(document.getElementById('calendar-form-start-time').value).toBe(
        '09:00',
      );
      expect(document.getElementById('calendar-form-end-time').value).toBe(
        '10:00',
      );
      // A single event has no occurrence to edit on its own.
      expect(document.querySelector('.calendar-edit-scope')).toBeNull();

      button(t('common.save')).click();
      await waitForCondition(() => rpcCalls('calendar.update').length === 1);

      // Regression: resaving without edits must not shift the event by the
      // zone offset.
      expect(rpcCalls('calendar.update')).toEqual([
        {
          id: 'evt-1',
          title: 'Dentist',
          description: null,
          location: 'Main Street 4',
          start: '2026-09-23T09:00:00',
          end: '2026-09-23T10:00:00',
          rrule: null,
        },
      ]);
    });

    it('changes only the chosen occurrence of a repeating event', async () => {
      serveWindow(weeklyStandup());
      mountedComponent = await mountCalendarView();
      document.querySelector('.calendar-cell .calendar-entry').click();
      flushSync();
      button(t('common.edit')).click();
      flushSync();

      // The series is edited by default, with its repetition.
      expect(document.getElementById('calendar-form-freq')).not.toBeNull();
      const [series, only] = document.querySelectorAll(
        '.calendar-edit-scope input[type="radio"]',
      );
      expect(series.checked).toBe(true);
      only.click();
      flushSync();
      // One occurrence keeps the series' repetition.
      expect(document.getElementById('calendar-form-freq')).toBeNull();

      typeInto('calendar-form-title-input', 'Standup with guests');
      typeInto('calendar-form-start-time', '10:00');
      button(t('common.save')).click();
      await waitForCondition(() => rpcCalls('calendar.update').length === 1);

      // A new start keeps the occurrence's length.
      expect(rpcCalls('calendar.update')).toEqual([
        {
          id: 'evt-1_20260923T0900',
          title: 'Standup with guests',
          description: null,
          location: null,
          start: '2026-09-23T10:00:00',
          end: '2026-09-23T10:30:00',
        },
      ]);
    });

    it('shows the Agent jobs of an event on its entry instead of as Schedule Runs', async () => {
      const window = weeklyStandup();
      window.cron = [
        {
          job_id: 'cron-1',
          name: 'Prepare the agenda',
          fire_at: '2026-09-23T08:30:00+00:00',
          schedule_type: 'event',
          event_id: 'evt-1',
          occurrence_id: 'evt-1_20260923T0900',
        },
      ];
      serveWindow(window, [eventJob()]);
      mountedComponent = await mountCalendarView();

      // The job runs from its event, not as a separate Schedule entry.
      expect(document.querySelector('.calendar-entry--cron')).toBeNull();
      const entry = document.querySelector('.calendar-cell .calendar-entry');
      expect(entry.querySelector('.calendar-entry-jobs').textContent).toBe('1');
      focusWithKeyboard(entry);
      expect(tooltipRows()).toContainEqual([t('calendar.jobs.heading'), '1']);
      const [label, value] = tooltipRows().find(([name]) =>
        name.startsWith('30 minutes before start'),
      );
      expect(label).toContain('08:30');
      expect(value).toBe('main: Prepare the agenda');

      entry.click();
      await waitForCondition(
        () =>
          document.querySelector('[data-testid="calendar-job-cron-1"]') !==
          null,
      );
      expect(document.querySelector('.calendar-jobs h3').textContent).toBe(
        t('calendar.jobs.heading'),
      );
    });

    it('explains Schedule Runs, hidden entries and layers in tooltips', async () => {
      const hour = (value) => `2026-09-23T${value}:00:00+00:00`;
      serveWindow(
        calendarWindow({
          occurrences: ['07', '08', '09', '10', '11'].map((value, index) =>
            occurrence(`Event ${index + 1}`, hour(value), hour(value)),
          ),
          cron: [
            {
              job_id: 'job-1',
              name: 'Nightly digest',
              fire_at: hour('06'),
              schedule_type: 'cron',
            },
          ],
        }),
      );
      mountedComponent = await mountCalendarView();

      // A Schedule Run says that clicking opens its Schedule.
      focusWithKeyboard(document.querySelector('.calendar-entry--cron'));
      expect(
        document.querySelector('#app-tooltip .app-tooltip__title').textContent,
      ).toBe('Nightly digest');
      expect(
        document.querySelector('#app-tooltip .app-tooltip__text').textContent,
      ).toBe(t('calendar.details.cronLead'));

      // The "+N" marker lists the entries the cell has no room for.
      const more = document.querySelector('.calendar-entry-more');
      expect(more.textContent).toBe('+2');
      document.activeElement.blur();
      more.dispatchEvent(new MouseEvent('pointerenter'));
      await vi.waitFor(() =>
        expect(
          document.querySelector('#app-tooltip .app-tooltip__title')
            ?.textContent,
        ).toBe(t('calendar.details.more')),
      );
      expect(tooltipRows().map(([, value]) => value)).toEqual([
        'Event 4',
        'Event 5',
      ]);
      more.dispatchEvent(new MouseEvent('pointerleave'));

      // A layer chip is a pressed toggle that counts its entries in the view.
      const chip = document.querySelector('.calendar-chip--local');
      expect(chip.getAttribute('aria-pressed')).toBe('true');
      focusWithKeyboard(chip);
      expect(
        document.querySelector('#app-tooltip .app-tooltip__title').textContent,
      ).toBe(t('calendar.layer.local'));
      expect(tooltipRows()).toEqual([
        [t('calendar.layer.inView'), '5'],
        ['', t('calendar.layer.shown')],
      ]);
      chip.click();
      flushSync();
      expect(chip.getAttribute('aria-pressed')).toBe('false');
    });

    it('deletes only the chosen occurrence of a repeating event by its id', async () => {
      serveWindow(weeklyStandup(), [eventJob()]);
      mountedComponent = await mountCalendarView();
      // The entry's card names how the series repeats.
      focusWithKeyboard(
        document.querySelector('.calendar-cell .calendar-entry'),
      );
      expect(tooltipRows()).toContainEqual([
        t('calendar.form.recurrence'),
        t('calendar.form.freqWeekly'),
      ]);
      document.querySelector('.calendar-cell .calendar-entry').click();
      flushSync();

      // The footer deletes the event; each Agent job has its own Delete.
      button(
        t('common.delete'),
        document.querySelector('.modal-footer'),
      ).click();
      flushSync();
      // Deleting the whole event takes its Agent jobs along.
      expect(document.querySelector('.calendar-delete-jobs').textContent).toBe(
        t('calendar.deleteJob'),
      );
      document
        .querySelectorAll('.calendar-delete-choice input[type="radio"]')[1]
        .click();
      flushSync();
      expect(document.querySelector('.calendar-delete-jobs')).toBeNull();
      button(
        t('calendar.deleteOccurrence'),
        document.querySelector('.modal-footer'),
      ).click();
      await waitForCondition(() => rpcCalls('calendar.delete').length > 0);

      expect(rpcCalls('calendar.delete')).toEqual([
        { id: 'evt-1_20260923T0900' },
      ]);
      expect(rpcCalls('calendar.update')).toEqual([]);
    });
  });
});
