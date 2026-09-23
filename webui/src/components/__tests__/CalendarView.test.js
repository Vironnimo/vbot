// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../lib/i18n.js';
import { rpcBackedApiMock } from './apiMock.js';

const rpcMock = vi.fn();

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => rpcBackedApiMock(rpcMock));

const { default: CalendarView } = await import('../CalendarView.svelte');

function emptyWindow() {
  return { events: [], occurrences: [], cron: [], system_timezone: 'UTC' };
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

function showView(label) {
  [...document.querySelectorAll('[role="tab"]')]
    .find((tab) => tab.textContent.trim() === label)
    .click();
  flushSync();
}

// Intl separates date ranges with (thin) spaces; compare plain text.
function plainText(element) {
  return element.textContent.replace(/\s+/g, ' ').trim();
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
    rpcMock.mockImplementation((method) => {
      if (method === 'calendar.window') {
        return Promise.resolve(emptyWindow());
      }
      return Promise.reject(new Error(`Unexpected RPC: ${method}`));
    });
  });

  afterEach(() => {
    if (mountedComponent) {
      unmount(mountedComponent);
      mountedComponent = null;
    }
  });

  it('renders the month grid when nothing is scheduled', async () => {
    mountedComponent = await mountCalendarView();

    // Regression: an empty window used to replace the whole calendar with an
    // empty state; the grid itself shows that nothing is scheduled.
    expect(document.querySelector('.calendar-grid')).not.toBeNull();
    expect(document.querySelectorAll('.calendar-weekday')).toHaveLength(7);
    expect(document.querySelectorAll('.calendar-cell')).toHaveLength(42);
    expect(document.querySelector('.empty-state__title')).toBeNull();
  });

  it('opens the event form from the toolbar action', async () => {
    mountedComponent = await mountCalendarView();

    document.querySelector('.calendar-toolbar-right .btn-primary').click();
    flushSync();

    expect(document.querySelector('.calendar-form')).not.toBeNull();
  });

  it('insets the event form inside a padded modal body', async () => {
    mountedComponent = await mountCalendarView();

    document.querySelector('.calendar-toolbar-right .btn-primary').click();
    flushSync();

    // The Modal shell renders body snippets directly; callers own the padded
    // `.modal-body` wrapper. Without it the form sits flush against the modal
    // edges.
    expect(document.querySelector('.modal-body .calendar-form')).not.toBeNull();
  });

  it('associates each event-form label with its control', async () => {
    mountedComponent = await mountCalendarView();

    document.querySelector('.calendar-toolbar-right .btn-primary').click();
    flushSync();

    const titleLabel = document.querySelector(
      '.calendar-form label[for="calendar-form-title-input"]',
    );
    const titleInput = document.querySelector(
      '.calendar-form input#calendar-form-title-input',
    );
    const dateLabel = document.querySelector(
      '.calendar-form label[for="calendar-form-date"]',
    );
    const dateInput = document.querySelector(
      '.calendar-form input#calendar-form-date',
    );

    expect(titleLabel).not.toBeNull();
    expect(titleInput).not.toBeNull();
    expect(dateLabel).not.toBeNull();
    expect(dateInput).not.toBeNull();
  });

  it('sets recurrence and its end through the shared choice fields', async () => {
    mountedComponent = await mountCalendarView();

    document.querySelector('.calendar-toolbar-right .btn-primary').click();
    flushSync();

    // The labels still name the Dropdown triggers.
    const freqTrigger = document.getElementById('calendar-form-freq');
    expect(freqTrigger.tagName).toBe('BUTTON');
    expect(
      document.querySelector('.calendar-form label[for="calendar-form-freq"]'),
    ).not.toBeNull();
    expect(document.querySelector('.calendar-form select')).toBeNull();
    expect(document.querySelector('.calendar-weekday-picker')).toBeNull();

    chooseOption('calendar-form-freq', 'Weekly');
    expect(freqTrigger.textContent.trim()).toBe('Weekly');
    expect(document.querySelector('.calendar-weekday-picker')).not.toBeNull();

    chooseOption('calendar-form-end-mode', 'After');
    expect(
      document.querySelector('.calendar-form-ends input[type="number"]'),
    ).not.toBeNull();

    chooseOption('calendar-form-end-mode', 'On date');
    expect(
      document.querySelector('.calendar-form-ends input[type="date"]'),
    ).not.toBeNull();
  });

  it('opens the create form from clicking an empty cell surface', async () => {
    mountedComponent = await mountCalendarView();

    const surface = document.querySelector('.calendar-cell-surface');
    expect(surface).not.toBeNull();
    surface.click();
    flushSync();

    expect(document.querySelector('.calendar-form')).not.toBeNull();
  });

  it('marks today as a highlighted cell surface', async () => {
    mountedComponent = await mountCalendarView();

    expect(document.querySelector('.calendar-cell.is-today')).not.toBeNull();
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
      showView('Week');

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

    it('names the shown day once in the day view', async () => {
      mountedComponent = await mountCalendarView();
      showView('Day');

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
  });

  it('opens the detail modal from an event entry, not the create form', async () => {
    const now = new Date();
    const todayNoonUtc = new Date(
      Date.UTC(
        now.getUTCFullYear(),
        now.getUTCMonth(),
        now.getUTCDate(),
        12,
        0,
        0,
      ),
    ).toISOString();
    rpcMock.mockImplementation((method) => {
      if (method === 'calendar.window') {
        return Promise.resolve({
          events: [{ id: 'evt-1', title: 'Standup', rrule: null }],
          occurrences: [
            {
              event_id: 'evt-1',
              title: 'Standup',
              all_day: false,
              recurring: false,
              notes: null,
              start_utc: todayNoonUtc,
              end_utc: todayNoonUtc,
              start_date: null,
              end_date: null,
              occurrence_start: todayNoonUtc,
            },
          ],
          cron: [],
          system_timezone: 'UTC',
        });
      }
      return Promise.reject(new Error(`Unexpected RPC: ${method}`));
    });
    mountedComponent = await mountCalendarView();

    const entry = document.querySelector('.calendar-cell .calendar-entry');
    expect(entry).not.toBeNull();
    entry.click();
    flushSync();

    // Regression: clicking an entry used to bubble to the create surface, so
    // the form opened instead of the detail modal.
    expect(document.querySelector('.calendar-detail')).not.toBeNull();
    expect(document.querySelector('.calendar-form')).toBeNull();
  });
});
