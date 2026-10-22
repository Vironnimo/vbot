// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: CopyButton } = await import('../CopyButton.svelte');

describe('CopyButton', () => {
  let mountedComponent;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    window.history.replaceState({}, '', '/');
    delete window.pywebview;
    vi.restoreAllMocks();
  });

  function render(props) {
    mountedComponent = mount(CopyButton, { target: document.body, props });
    flushSync();
    return document.body.querySelector('button');
  }

  it.each(['browser', 'desktop'])(
    'copies verbatim through the %s clipboard and shows feedback',
    async (accessor) => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      configureClipboard(accessor, writeText);

      const button = render({ text: 'a verbatim - line' });
      expect(button.classList.contains('copy-button')).toBe(true);
      expect(button.getAttribute('aria-label')).toBe('Copy');

      button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flushAsync();

      expect(writeText).toHaveBeenCalledWith('a verbatim - line');
      expect(button.getAttribute('aria-label')).toBe('Copied');
    },
  );

  it('honors caller-supplied labels and passthrough class', () => {
    const button = render({
      text: 'x',
      label: 'Copy log line',
      copiedLabel: 'Copied!',
      class: 'logs-entry__copy',
    });

    expect(button.getAttribute('aria-label')).toBe('Copy log line');
    expect(button.classList.contains('logs-entry__copy')).toBe(true);
  });

  it('disables itself when there is no text to copy', () => {
    const button = render({ text: '' });
    expect(button.disabled).toBe(true);
  });

  it.each(['browser', 'desktop'])(
    'keeps the resting state when the %s clipboard refuses access',
    async (accessor) => {
      const writeText = vi.fn().mockRejectedValue(new Error('denied'));
      configureClipboard(accessor, writeText);

      const button = render({ text: 'data' });
      button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flushAsync();

      expect(writeText).toHaveBeenCalled();
      expect(button.getAttribute('aria-label')).toBe('Copy');
    },
  );
});

function configureClipboard(accessor, writeText) {
  const desktop = accessor === 'desktop';
  window.history.replaceState({}, '', desktop ? '/?accessor=desktop' : '/');
  Object.defineProperty(navigator, 'clipboard', {
    value: desktop ? undefined : { writeText },
    configurable: true,
  });
  if (desktop) window.pywebview = { api: { setClipboardText: writeText } };
}

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}
