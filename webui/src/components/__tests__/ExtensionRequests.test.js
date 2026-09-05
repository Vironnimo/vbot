// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { init } from '../../lib/i18n.js';

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
let component;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.clearAllMocks();
  vi.useRealTimers();
});

it.each([false, true])(
  'submits a response when polling removes it first: %s',
  async (removedWhileSending) => {
    vi.useFakeTimers();
    init('en');
    listRequests.mockResolvedValue({
      requests: [
        {
          id: 'request',
          extension: 'mcp',
          connection: 'blender',
          response_operation: 'respond',
          kind: 'elicitation',
          payload: {
            message: 'test-owned-question',
            requestedSchema: {
              properties: { name: { type: 'string', title: 'Name' } },
            },
          },
        },
      ],
    });
    let finishResponse;
    operation.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishResponse = resolve;
        }),
    );
    component = mount(ExtensionRequests, { target: document.body });
    await vi.waitFor(() => {
      flushSync();
      expect(document.querySelector('button')).not.toBeNull();
    });
    document.querySelector('button').click();
    flushSync();
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog.textContent).toContain('test-owned-question');
    const input = dialog.querySelector('input');
    input.value = 'user-answer';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    [...dialog.querySelectorAll('button')]
      .find((button) => button.textContent.includes('Send response'))
      .click();
    await vi.waitFor(() =>
      expect(operation).toHaveBeenCalledWith('mcp', 'respond', {
        request_id: 'request',
        response: { action: 'accept', content: { name: 'user-answer' } },
      }),
    );
    if (removedWhileSending) {
      listRequests.mockResolvedValue({ requests: [] });
      await vi.advanceTimersByTimeAsync(2000);
      flushSync();
      expect(document.querySelector('[role="dialog"]')).toBeNull();
    }
    finishResponse({ answered: true });
    await vi.waitFor(() => {
      flushSync();
      expect(document.querySelector('[role="dialog"]')).toBeNull();
      expect(document.querySelector('[role="alert"]')).toBeNull();
    });
  },
);
