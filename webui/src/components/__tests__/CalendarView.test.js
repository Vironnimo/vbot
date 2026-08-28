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
});
