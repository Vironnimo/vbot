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
  timezone = 'UTC',
} = {}) {
  return { events, occurrences, cron: [], system_timezone: timezone };
}

function serveWindow(window) {
  rpcMock.mockImplementation((method) => {
    if (method === 'calendar.window') {
      return Promise.resolve(window);
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
        .querySelector(`button[aria-label="${t('calendar.prev')}"]`)
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
      for (const id of ['calendar-form-title-input', 'calendar-form-date']) {
        expect(
          document.querySelector(`.calendar-form label[for="${id}"]`),
        ).not.toBeNull();
        expect(document.querySelector(`.calendar-form input#${id}`)).not.toBe(
          null,
        );
      }
      expect(document.getElementById('calendar-form-date').value).toBe(
        '2026-09-23',
      );

      button(t('common.cancel')).click();
      flushSync();
      expect(document.querySelector('.calendar-form')).toBeNull();

      // The first surface of the September grid is Monday, August 31.
      document.querySelector('.calendar-cell-surface').click();
      flushSync();
      expect(document.getElementById('calendar-form-date').value).toBe(
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

      expect(rpcCalls('calendar.create')).toEqual([
        {
          title: 'Standup',
          notes: null,
          all_day: false,
          start: '2026-09-23T09:00:00',
          duration_minutes: 60,
          rrule: {
            freq: 'weekly',
            interval: 1,
            by_weekday: ['mo', 'tu', 'we', 'th', 'fr'],
            count: 10,
          },
        },
      ]);
    });

    it('opens an entry in the detail modal and saves an edit in the server wall clock', async () => {
      const start = '2026-09-23T07:00:00+00:00';
      serveWindow(
        calendarWindow({
          timezone: 'Europe/Berlin',
          events: [
            {
              id: 'evt-1',
              title: 'Dentist',
              notes: null,
              location: null,
              all_day: false,
              start_utc: start,
              start_local: null,
              tz_name: null,
              start_date: null,
              duration_minutes: 60,
              duration_days: null,
              rrule: null,
              exdates: [],
            },
          ],
          occurrences: [
            {
              event_id: 'evt-1',
              title: 'Dentist',
              all_day: false,
              recurring: false,
              notes: null,
              start_utc: start,
              end_utc: '2026-09-23T08:00:00+00:00',
              start_date: null,
              end_date: null,
              occurrence_start: start,
            },
          ],
        }),
      );
      mountedComponent = await mountCalendarView();

      document.querySelector('.calendar-cell .calendar-entry').click();
      flushSync();

      // Regression: clicking an entry used to bubble to the create surface, so
      // the form opened instead of the detail modal.
      expect(document.querySelector('.calendar-detail')).not.toBeNull();
      expect(document.querySelector('.calendar-form')).toBeNull();

      button(t('common.edit')).click();
      flushSync();
      // 07:00 UTC is 09:00 in Berlin; the form presents that wall clock.
      expect(document.getElementById('calendar-form-date').value).toBe(
        '2026-09-23',
      );
      expect(document.getElementById('calendar-form-time').value).toBe('09:00');

      button(t('common.save')).click();
      await waitForCondition(() => rpcCalls('calendar.update').length === 1);

      // Regression: resaving without edits must not shift the event by the
      // zone offset.
      expect(rpcCalls('calendar.update')).toEqual([
        {
          id: 'evt-1',
          title: 'Dentist',
          notes: null,
          all_day: false,
          start: '2026-09-23T09:00:00',
          duration_minutes: 60,
          rrule: null,
        },
      ]);
    });

    it('deletes only the chosen occurrence of a recurring event additively', async () => {
      serveWindow(
        calendarWindow({
          events: [
            {
              id: 'evt-1',
              title: 'Standup',
              rrule: { freq: 'weekly', interval: 1 },
            },
          ],
          occurrences: [
            {
              event_id: 'evt-1',
              title: 'Standup',
              all_day: false,
              recurring: true,
              notes: null,
              start_utc: '2026-09-23T09:00:00+00:00',
              end_utc: '2026-09-23T09:30:00+00:00',
              start_date: null,
              end_date: null,
              occurrence_start: '2026-09-23T09:00:00',
            },
          ],
        }),
      );
      mountedComponent = await mountCalendarView();
      document.querySelector('.calendar-cell .calendar-entry').click();
      flushSync();

      button(t('common.delete')).click();
      flushSync();
      document
        .querySelectorAll('.calendar-delete-choice input[type="radio"]')[1]
        .click();
      flushSync();
      button(
        t('calendar.deleteOccurrence'),
        document.querySelector('.modal-footer'),
      ).click();
      await waitForCondition(() => rpcCalls('calendar.add_exdate').length > 0);

      expect(rpcCalls('calendar.add_exdate')).toEqual([
        { id: 'evt-1', occurrence_start: '2026-09-23T09:00:00' },
      ]);
      // Regression: excluding used to re-send the whole exdates array through
      // an update, which could drop a concurrent tab's exclusion.
      expect(rpcCalls('calendar.update')).toEqual([]);
      expect(rpcCalls('calendar.delete')).toEqual([]);
    });
  });
});
