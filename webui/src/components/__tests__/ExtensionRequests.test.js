// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { setApplicationTimeZone } from '../../lib/dateTimePrefs.svelte.js';
import { init, t } from '../../lib/i18n.js';
import { formatAbsoluteTime, formatRelativeTime } from '../../lib/timeText.js';

const listRequests = vi.fn();
const operation = vi.fn();
vi.mock(
  'svelte',
  async () => import('../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  listExtensionRequests: (...args) => listRequests(...args),
  extensionOperation: (...args) => operation(...args),
}));
const { default: ExtensionRequests } =
  await import('../ExtensionRequests.svelte');

init('en');
const REVIEW = t('extensions.reviewInput');
const SEND = t('extensions.sendResponse');
const DECLINE = t('extensions.declineInput');
const CANCEL = t('extensions.cancelInput');
const namePayload = {
  message: 'test-owned-question',
  requestedSchema: {
    properties: { name: { type: 'string', title: 'Name' } },
  },
};
let component;
let listeners = [];

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  listeners = [];
  document.body.innerHTML = '';
  vi.clearAllMocks();
  vi.useRealTimers();
  setApplicationTimeZone('UTC');
});

function subscribeInvalidations(listener) {
  listeners.push(listener);
  return () => listeners.splice(listeners.indexOf(listener), 1);
}

// Delivers one App Extension invalidation (`{owner, change}`).
function invalidate(owner, resource = null) {
  const change = resource ? { resource, ids: ['request'], revision: 1 } : null;
  for (const listener of [...listeners]) listener({ owner, change });
}

function mountRequests() {
  component = mount(ExtensionRequests, {
    target: document.body,
    props: { subscribeInvalidations },
  });
}

// Mounts the request surface with one pending elicitation and opens it.
async function openRequest(payload, fields = {}) {
  listRequests.mockResolvedValue({
    requests: [
      {
        id: 'request',
        extension: 'mcp',
        connection: 'blender',
        response_operation: 'respond',
        kind: 'elicitation',
        payload,
        ...fields,
      },
    ],
  });
  mountRequests();
  await vi.waitFor(() => {
    flushSync();
    expect(button(REVIEW)).toBeDefined();
  });
  button(REVIEW).click();
  flushSync();
  return document.querySelector('[role="dialog"]');
}

function button(label) {
  return [...document.querySelectorAll('button')].find(
    (item) => item.textContent.trim() === label,
  );
}

function field(dialog, label) {
  return [...dialog.querySelectorAll('.form-field')].find(
    (item) =>
      item.querySelector('label')?.textContent.replace('*', '').trim() ===
      label,
  );
}

function type(dialog, label, value) {
  const input = field(dialog, label).querySelector('input');
  expect(input).toBeTruthy();
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  flushSync();
}

function check(container, label) {
  [...container.querySelectorAll('[role="checkbox"]')]
    .find((item) => item.textContent.trim() === label)
    .click();
  flushSync();
}

// The Dropdown list is portaled to the document body.
function choose(dialog, label, option) {
  field(dialog, label).querySelector('.dropdown-trigger').click();
  flushSync();
  [...document.querySelectorAll('[role="option"]')]
    .find((item) => item.textContent.trim() === option)
    .click();
  flushSync();
}

function closed() {
  flushSync();
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.querySelector('[role="alert"]')).toBeNull();
  expect(document.querySelector('[role="status"]')).toBeNull();
}

describe('Extension requests', () => {
  it('reads pending inputs only on their changes and owner-less invalidations', async () => {
    vi.useFakeTimers();
    listRequests.mockResolvedValue({ requests: [] });
    mountRequests();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(listRequests).toHaveBeenCalledOnce();

    // Other Extension changes, such as a Swarm's, cannot alter the list.
    invalidate('swarm', 'swarms');
    invalidate('computer_use', 'control');
    await vi.advanceTimersByTimeAsync(0);
    expect(listRequests).toHaveBeenCalledOnce();

    // A pending-input change from any Extension, then a reconnect or reload.
    invalidate('mcp', 'pending_inputs');
    await vi.advanceTimersByTimeAsync(0);
    expect(listRequests).toHaveBeenCalledTimes(2);
    invalidate(null);
    await vi.advanceTimersByTimeAsync(0);
    expect(listRequests).toHaveBeenCalledTimes(3);
  });

  it('reads once more after a change that arrives during a read', async () => {
    let first;
    listRequests.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          first = resolve;
        }),
    );
    mountRequests();
    await vi.waitFor(() => expect(listRequests).toHaveBeenCalledOnce());
    listRequests.mockResolvedValue({
      requests: [
        {
          id: 'request',
          extension: 'mcp',
          response_operation: 'respond',
          kind: 'oauth',
          payload: {},
        },
      ],
    });
    invalidate('mcp', 'pending_inputs');
    invalidate('mcp', 'pending_inputs');
    first({ requests: [] });
    await vi.waitFor(() => {
      flushSync();
      expect(button(REVIEW)).toBeDefined();
    });
    expect(listRequests).toHaveBeenCalledTimes(2);
  });

  it('converts typed answers into an accepted response and closes the dialog', async () => {
    operation.mockResolvedValue({ answered: true });
    const expiresAt = new Date(Date.now() + 3 * 3600 * 1000).toISOString();
    const dialog = await openRequest(
      {
        message: 'test-owned-question',
        requestedSchema: {
          properties: {
            name: { type: 'string', title: 'Name' },
            count: { type: 'integer' },
            selected: { type: 'boolean' },
            values: { type: 'array' },
          },
        },
      },
      { expires_at: expiresAt },
    );
    // The time the request stops waiting, absolute and from now.
    expect(dialog.textContent).toContain(
      t('extensions.inputExpires', {
        time: formatAbsoluteTime(expiresAt),
        distance: formatRelativeTime(expiresAt),
      }),
    );
    expect(document.body.textContent).toContain(
      t('extensions.inputWaitingOne'),
    );
    expect(dialog.textContent).toContain(
      t('extensions.inputTitle', { name: 'blender' }),
    );
    expect(dialog.textContent).toContain('test-owned-question');
    type(dialog, 'Name', 'sentinel');
    type(dialog, 'count', '0');
    check(dialog, 'selected');
    type(dialog, 'values', '["a","b"]');
    button(SEND).click();
    await vi.waitFor(closed);
    expect(operation).toHaveBeenCalledExactlyOnceWith('mcp', 'respond', {
      request_id: 'request',
      response: {
        action: 'accept',
        content: {
          name: 'sentinel',
          count: 0,
          selected: true,
          values: ['a', 'b'],
        },
      },
    });
  });

  it('keeps a sent response when a refresh removes its request first', async () => {
    vi.useFakeTimers();
    let finishResponse;
    operation.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishResponse = resolve;
        }),
    );
    const dialog = await openRequest(namePayload);
    type(dialog, 'Name', 'user-answer');
    button(SEND).click();
    await vi.waitFor(() => expect(operation).toHaveBeenCalledOnce());
    listRequests.mockResolvedValue({ requests: [] });
    invalidate('mcp', 'pending_inputs');
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    finishResponse({ answered: true });
    await vi.advanceTimersByTimeAsync(0);
    closed();
  });

  it('renders choices and formats from the schema and sends only answers that fit', async () => {
    setApplicationTimeZone('Europe/Berlin');
    operation.mockResolvedValue({ answered: true });
    const dialog = await openRequest({
      message: 'test-owned-question',
      requestedSchema: {
        required: ['color', 'tags', 'email'],
        properties: {
          color: {
            type: 'string',
            title: 'Color',
            oneOf: [
              { const: 'r', title: 'Red' },
              { const: 'g', title: 'Green' },
            ],
            default: 'g',
          },
          size: {
            type: 'string',
            title: 'Size',
            enum: ['s', 'm'],
            enumNames: ['Small', 'Medium'],
          },
          tags: {
            type: 'array',
            title: 'Tags',
            items: {
              anyOf: [
                { const: 'a', title: 'Alpha' },
                { const: 'b', title: 'Beta' },
                { const: 'c', title: 'Gamma' },
              ],
            },
            minItems: 1,
            maxItems: 2,
          },
          email: { type: 'string', title: 'Email', format: 'email' },
          when: { type: 'string', title: 'When', format: 'date-time' },
          count: {
            type: 'integer',
            title: 'Count',
            minimum: 1,
            maximum: 5,
            default: 3,
          },
        },
      },
    });
    expect(field(dialog, 'Email').querySelector('input').type).toBe('email');
    expect(field(dialog, 'When').querySelector('input').type).toBe(
      'datetime-local',
    );
    expect(dialog.textContent).toContain(
      t('extensions.inputTimeZone', { zone: 'Europe/Berlin' }),
    );
    type(dialog, 'Email', 'not-an-address');
    type(dialog, 'Count', '9');
    for (const tag of ['Alpha', 'Beta', 'Gamma']) check(dialog, tag);
    button(SEND).click();
    flushSync();
    expect(operation).not.toHaveBeenCalled();
    expect(dialog.textContent).toContain(t('extensions.inputEmail'));
    expect(dialog.textContent).toContain(
      t('extensions.inputMaximum', { maximum: 5 }),
    );
    expect(dialog.textContent).toContain(
      t('extensions.inputChoicesMax', { count: 2 }),
    );

    type(dialog, 'Email', 'user@example.com');
    type(dialog, 'Count', '');
    type(dialog, 'Count', '3');
    check(dialog, 'Beta');
    choose(dialog, 'Size', 'Small');
    type(dialog, 'When', '2026-07-01T09:30');
    button(SEND).click();
    await vi.waitFor(closed);
    expect(operation).toHaveBeenCalledExactlyOnceWith('mcp', 'respond', {
      request_id: 'request',
      response: {
        action: 'accept',
        content: {
          color: 'g',
          size: 's',
          tags: ['a', 'c'],
          email: 'user@example.com',
          when: '2026-07-01T09:30:00+02:00',
          count: 3,
        },
      },
    });
  });

  it.each([
    [DECLINE, 'decline'],
    [CANCEL, 'cancel'],
  ])('answers %s without sending the draft', async (label, action) => {
    operation.mockResolvedValue({});
    const dialog = await openRequest(namePayload);
    type(dialog, 'Name', 'draft-sentinel');
    button(label).click();
    await vi.waitFor(closed);
    expect(operation).toHaveBeenCalledExactlyOnceWith('mcp', 'respond', {
      request_id: 'request',
      response: { action },
    });
  });

  it('shows where a requested page leads and accepts only when the user opens it', async () => {
    operation.mockResolvedValue({});
    const url = 'https://login.example.co.uk/authorize?state=test-owned';
    const dialog = await openRequest({
      mode: 'url',
      message: 'test-owned-link',
      url,
      elicitationId: 'test-owned-elicitation',
    });
    expect(dialog.textContent).toContain(
      t('extensions.urlRequest', { name: 'blender' }),
    );
    expect(dialog.textContent).toContain('test-owned-link');
    expect(dialog.querySelector('.requested-url__address').textContent).toBe(
      url,
    );
    await vi.waitFor(() => {
      flushSync();
      expect(dialog.querySelector('.requested-url__domain').textContent).toBe(
        'example.co.uk',
      );
    });
    expect(dialog.querySelector('.requested-url__site').textContent).toBe(
      'example.co.uk',
    );
    const links = dialog.querySelectorAll('a');
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe(url);
    expect(links[0].target).toBe('_blank');
    expect(links[0].rel).toBe('noopener noreferrer');
    expect(button(SEND)).toBeUndefined();
    expect(operation).not.toHaveBeenCalled();
    // jsdom cannot navigate; the browser would open the page in a new tab.
    const stay = (event) => event.preventDefault();
    document.addEventListener('click', stay);
    try {
      links[0].click();
    } finally {
      document.removeEventListener('click', stay);
    }
    await vi.waitFor(closed);
    expect(operation).toHaveBeenCalledExactlyOnceWith('mcp', 'respond', {
      request_id: 'request',
      response: { action: 'accept' },
    });
  });

  it.each([
    [
      'http://xn--bcher-kva.example/pay',
      ['extensions.urlInsecure', 'extensions.urlInternational'],
      true,
    ],
    ['javascript:alert(1)', ['extensions.urlInvalid'], false],
  ])('warns about the requested page %s', async (url, warnings, linked) => {
    const dialog = await openRequest({
      mode: 'url',
      message: 'test-owned-link',
      url,
    });
    for (const key of warnings) expect(dialog.textContent).toContain(t(key));
    expect(Boolean(dialog.querySelector('a'))).toBe(linked);
  });
});
