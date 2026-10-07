// @vitest-environment jsdom
// Shared setup for the SessionListDrawer suites: the Session API mock, a
// mount helper with reactive props, and queries for rows, markers, filters,
// the row menu and the confirm dialog. Import it before `svelte` so its
// module mocks apply.
import { afterEach, beforeEach, expect, vi } from 'vitest';
import { flushSync as svelteFlushSync, mount, unmount } from 'svelte';

import { init, t } from '../../lib/i18n.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';

const api = vi.hoisted(() => ({
  listSessions: vi.fn(),
  renameSession: vi.fn(),
  deleteSession: vi.fn(),
  setSessionCompactionPolicy: vi.fn(),
}));

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('$lib/api.js', () => api);

const { default: SessionListDrawer } =
  await import('../SessionListDrawer.svelte');

export { api };

export function flushSync() {
  return svelteFlushSync();
}

// A listed Session whose title is its id, so rows are addressable by id.
export function session(id, fields = {}) {
  return {
    id,
    title: id,
    created_at: '2026-05-09T00:00:00+00:00',
    ...fields,
  };
}

// Registers the per-test lifecycle and returns `mount(props)`, which mounts
// the drawer for Agent `alpha` on Session `session-1` and returns the
// reactive props bag, so a test can reassign props after mounting.
export function setupSessionListDrawerSuite() {
  let mountedComponent = null;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    // An untitled Session, as a new conversation is listed.
    api.listSessions.mockReset().mockResolvedValue({
      sessions: [{ id: 'session-1', created_at: '2026-05-09T00:00:00+00:00' }],
    });
    api.renameSession.mockReset().mockResolvedValue({
      title: 'Release planning',
    });
    api.setSessionCompactionPolicy.mockReset();
    api.deleteSession.mockReset().mockResolvedValue({
      agent_id: 'alpha',
      session_id: 'session-1',
      next_session_id: 'session-2',
    });
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  return {
    mount(overrides = {}) {
      const props = reactiveProps({
        agentId: 'alpha',
        currentSessionId: 'session-1',
        ...overrides,
      });
      mountedComponent = mount(SessionListDrawer, {
        target: document.body,
        props,
      });
      flushSync();
      return props;
    },
  };
}

export async function waitForCondition(check, attempts = 50) {
  for (let index = 0; index < attempts; index += 1) {
    if (check()) {
      return;
    }
    await Promise.resolve();
    flushSync();
  }
  throw new Error('Condition was not met in time');
}

export function rowCount() {
  return document.querySelectorAll('.session-row').length;
}

export function buttonByText(text) {
  return (
    [...document.body.querySelectorAll('button')].find(
      (button) => button.textContent.trim() === text,
    ) ?? null
  );
}

// Returns the row-select button of the Session row showing the given title.
export function sessionRowButton(title) {
  const button = [...document.querySelectorAll('.session-row__select')].find(
    (candidate) => candidate.textContent.includes(title),
  );
  expect(button, `session row not found: ${title}`).toBeTruthy();
  return button;
}

// Maps each row's name to the aria-labels of its markers.
export function markersByRow() {
  return Object.fromEntries(
    [...document.querySelectorAll('.session-row')].map((row) => [
      row.querySelector('.session-row__name').textContent.trim(),
      [...row.querySelectorAll('[data-session-marker]')].map((marker) =>
        marker.getAttribute('aria-label'),
      ),
    ]),
  );
}

// Opens the header filter dropdown and returns its portaled panel.
export function openFilterMenu() {
  document.querySelector('.session-drawer__filter-trigger').click();
  flushSync();
  const menu = document.querySelector('.session-drawer__filter-menu');
  expect(menu, 'filter menu did not open').toBeTruthy();
  return menu;
}

// Returns the filter dropdown's switch for one filter key, such as `cron`.
export function filterSwitch(filter) {
  const label = t(`sessions.filters.${filter}`);
  const toggle = [
    ...document.querySelectorAll(
      '.session-drawer__filter-menu [role="switch"]',
    ),
  ].find((candidate) => candidate.getAttribute('aria-label') === label);
  expect(toggle, `filter switch not found: ${label}`).toBeTruthy();
  return toggle;
}

// Opens the row menu of the Session titled `title` (the first row by
// default) and chooses the item with the given text.
export function chooseRowAction(itemText, title) {
  const row = title
    ? sessionRowButton(title).closest('.session-row')
    : document.querySelector('.session-row');
  row.querySelector('.session-row__menu-trigger').click();
  flushSync();
  const item = [
    ...document.querySelectorAll('.context-menu [role="menuitem"]'),
  ].find((candidate) => candidate.textContent.trim() === itemText);
  expect(item, `row menu item not found: ${itemText}`).toBeTruthy();
  item.click();
  flushSync();
}

// Clicks a button in the open ConfirmDialog by its label.
export function confirmDialog(label) {
  const footer = document.querySelector('.modal-footer');
  expect(footer, 'confirm dialog not open').toBeTruthy();
  const button = [...footer.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
  expect(button, `confirm button not found: ${label}`).toBeTruthy();
  button.click();
}
