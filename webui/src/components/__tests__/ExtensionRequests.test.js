// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init, t } from '../../lib/i18n.js';

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
const REVIEW = t('extensions.reviewInput', 'Review request');
const SEND = t('extensions.sendResponse', 'Send response');
const DECLINE = t('extensions.declineInput', 'Decline');
const namePayload = {
  message: 'test-owned-question',
  requestedSchema: {
    properties: { name: { type: 'string', title: 'Name' } },
  },
};
let component;

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.clearAllMocks();
  vi.useRealTimers();
});

// Mounts the request surface with one pending elicitation and opens it.
async function openRequest(payload) {
  listRequests.mockResolvedValue({
    requests: [
      {
        id: 'request',
        extension: 'mcp',
        connection: 'blender',
        response_operation: 'respond',
        kind: 'elicitation',
        payload,
      },
    ],
  });
  component = mount(ExtensionRequests, { target: document.body });
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
    (item) => item.querySelector('label')?.textContent.trim() === label,
  );
}

function type(dialog, label, value) {
  const input = field(dialog, label).querySelector('input');
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
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
  it('converts typed answers into an accepted response and closes the dialog', async () => {
    operation.mockResolvedValue({ answered: true });
    const dialog = await openRequest({
      message: 'test-owned-question',
      requestedSchema: {
        properties: {
          name: { type: 'string', title: 'Name' },
          count: { type: 'integer' },
          selected: { type: 'boolean' },
          values: { type: 'array' },
        },
      },
    });
    expect(document.body.textContent).toContain(
      t(
        'extensions.inputWaiting',
        '{count} Extension requests need your response.',
        {
          count: 1,
        },
      ),
    );
    expect(dialog.textContent).toContain(
      t('extensions.inputTitle', 'Request from {name}', { name: 'blender' }),
    );
    expect(dialog.textContent).toContain('test-owned-question');
    type(dialog, 'Name', 'sentinel');
    type(dialog, 'count', '0');
    choose(dialog, 'selected', t('common.no', 'No'));
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
          selected: false,
          values: ['a', 'b'],
        },
      },
    });
  });

  it('keeps a sent response when polling removes its request first', async () => {
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
    await vi.advanceTimersByTimeAsync(2000);
    flushSync();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    finishResponse({ answered: true });
    await vi.advanceTimersByTimeAsync(0);
    closed();
  });

  it('declines without sending the draft', async () => {
    operation.mockResolvedValue({});
    const dialog = await openRequest(namePayload);
    type(dialog, 'Name', 'draft-sentinel');
    button(DECLINE).click();
    await vi.waitFor(closed);
    expect(operation).toHaveBeenCalledExactlyOnceWith('mcp', 'respond', {
      request_id: 'request',
      response: { action: 'decline' },
    });
  });

  it.each([
    ['https://example.com/authorize', 'https://example.com/authorize'],
    ['javascript:alert(1)', null],
  ])(
    'links the requested page %s only when it is a web address',
    async (url, expected) => {
      const dialog = await openRequest({ message: 'test-owned-link', url });
      expect(dialog.querySelector('a')?.getAttribute('href') ?? null).toBe(
        expected,
      );
    },
  );
});
