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

  it('renders the empty state with its action button when nothing is scheduled', async () => {
    mountedComponent = mount(CalendarView, { target: document.body });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.empty-state__title') !== null,
    );

    expect(document.querySelector('.empty-state__title')?.textContent).toBe(
      'Nothing scheduled',
    );
    // Regression: the empty-state action is a snippet prop. Passing a plain
    // array here crashed the whole view with "e is not a function".
    const action = document.querySelector('.empty-state__actions button');
    expect(action).not.toBeNull();
    expect(action.textContent).toContain('New event');
  });

  it('opens the event form from the empty-state action', async () => {
    mountedComponent = mount(CalendarView, { target: document.body });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.empty-state__actions button') !== null,
    );

    document.querySelector('.empty-state__actions button').click();
    flushSync();

    expect(document.querySelector('.calendar-form')).not.toBeNull();
  });

  it('associates each event-form label with its control', async () => {
    mountedComponent = mount(CalendarView, { target: document.body });
    flushSync();
    await waitForCondition(
      () => document.querySelector('.empty-state__actions button') !== null,
    );

    document.querySelector('.empty-state__actions button').click();
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
