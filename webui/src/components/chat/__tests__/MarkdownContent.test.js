// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import { init } from '../../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: MarkdownContent } = await import('../MarkdownContent.svelte');

describe('MarkdownContent', () => {
  let mountedComponent;
  let writeText;

  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }
    document.body.innerHTML = '';
    vi.restoreAllMocks();
  });

  it('renders a fenced code header and copies only the code', async () => {
    mountedComponent = mount(MarkdownContent, {
      target: document.body,
      props: {
        source: '```python\ndef answer():\n    return 42\n```',
        class: 'msg-markdown',
      },
    });
    flushSync();

    expect(document.querySelector('.msg-code__language').textContent).toBe(
      'python',
    );
    const copyButton = document.querySelector('.msg-code__copy');
    expect(copyButton).toBeTruthy();
    expect(copyButton.getAttribute('aria-label')).toBe('Copy code');

    copyButton.click();
    await flushAsync();

    expect(writeText).toHaveBeenCalledWith('def answer():\n    return 42\n');
  });

  it('uses the localized plain-text label when no language is declared', () => {
    mountedComponent = mount(MarkdownContent, {
      target: document.body,
      props: { source: '```\nplain\n```' },
    });
    flushSync();

    expect(document.querySelector('.msg-code__language').textContent).toBe(
      'text',
    );
  });

  it('withholds code copy while a streaming fence is incomplete', () => {
    mountedComponent = mount(MarkdownContent, {
      target: document.body,
      props: {
        source: '```js\nconst partial = true;',
        streaming: true,
      },
    });
    flushSync();

    expect(document.querySelector('.msg-code__language').textContent).toBe(
      'js',
    );
    expect(document.querySelector('.msg-code__copy')).toBeNull();
  });

  it('keeps delivered HTML as a link with one external hint and no menu button', async () => {
    mountedComponent = mount(MarkdownContent, {
      target: document.body,
      props: {
        source:
          '[site.HTML](/api/files/signed-token) [report.pdf](/api/files/pdf-token) [remote.html](https://example.com/site.html)',
        class: 'msg-markdown',
      },
    });
    flushSync();
    expect(document.querySelectorAll('[data-file-external]')).toHaveLength(1);
    expect(document.querySelector('button')).toBeNull();
    const external = document.querySelector('[data-file-external]');
    expect(external.getAttribute('href')).toBe('/api/files/signed-token');
    expect(external.target).toBe('_blank');
    expect(external.rel).toContain('noopener');
    expect(external.getAttribute('aria-label')).toContain('site.HTML');
    const menu = document.querySelector(
      'a[data-preview-file]:not([data-file-external])',
    );
    expect(menu.dataset.previewFile).toBe('/api/files/signed-token');
    expect(menu.dataset.fileName).toBe('site.HTML');
    expect(menu.getAttribute('aria-haspopup')).toBe('menu');
    await unmount(mountedComponent);
    mountedComponent = null;
    expect(document.querySelector('[data-file-external]')).toBeNull();
  });
});

async function flushAsync() {
  for (let index = 0; index < 5; index += 1) {
    await Promise.resolve();
    flushSync();
  }
}
