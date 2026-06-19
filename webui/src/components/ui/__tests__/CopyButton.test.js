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
    vi.restoreAllMocks();
  });

  function render(props) {
    mountedComponent = mount(CopyButton, { target: document.body, props });
    flushSync();
    return document.body.querySelector('button');
  }

  it('writes the provided text verbatim and shows copied feedback', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    const button = render({ text: 'a verbatim - line' });
    expect(button.classList.contains('copy-button')).toBe(true);
    expect(button.getAttribute('title')).toBe('Copy');

    button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('a verbatim - line');
    expect(button.getAttribute('title')).toBe('Copied');
  });

  it('honors caller-supplied labels and passthrough class', () => {
    const button = render({
      text: 'x',
      label: 'Copy log line',
      copiedLabel: 'Copied!',
      class: 'logs-entry__copy',
    });

    expect(button.getAttribute('title')).toBe('Copy log line');
    expect(button.getAttribute('aria-label')).toBe('Copy log line');
    expect(button.classList.contains('logs-entry__copy')).toBe(true);
  });

  it('disables itself when there is no text to copy', () => {
    const button = render({ text: '' });
    expect(button.disabled).toBe(true);
  });

  it('stays in the resting state when clipboard access is blocked', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });

    const button = render({ text: 'data' });
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await flushAsync();

    expect(writeText).toHaveBeenCalled();
    expect(button.getAttribute('title')).toBe('Copy');
  });
});

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}
