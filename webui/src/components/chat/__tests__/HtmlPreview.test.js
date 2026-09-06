// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import HtmlPreview from '../HtmlPreview.svelte';
import { init } from '../../../lib/i18n.js';

const { open, revision } = vi.hoisted(() => ({
  open: vi.fn(),
  revision: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  openFilePreview: open,
  getFilePreviewRevision: revision,
}));
let component;
const result = {
  token: 'capability',
  url: '/api/preview-assets/capability/index.html',
  source: '/site/index.html',
  root: '/site',
  filename: 'index.html',
  revision: 'one',
};
beforeEach(() => {
  init('en');
  vi.useFakeTimers();
  open.mockReset().mockResolvedValue(result);
  revision.mockReset().mockResolvedValue({ revision: 'one' });
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.useRealTimers();
});
async function settle() {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve();
    flushSync();
  }
}

describe('HtmlPreview', () => {
  it('refreshes only on changes, pauses polling and cleans up on unmount', async () => {
    component = mount(HtmlPreview, {
      target: document.body,
      props: { request: { source: '/site/index.html' } },
    });
    await settle();
    const frame = document.querySelector('iframe');
    const reload = vi.spyOn(frame, 'src', 'set');
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    await vi.advanceTimersByTimeAsync(1500);
    expect(reload).not.toHaveBeenCalled();
    revision.mockResolvedValue({ revision: 'two' });
    await vi.advanceTimersByTimeAsync(1500);
    expect(reload).toHaveBeenCalledExactlyOnceWith(result.url);
    document.querySelector('[role="switch"]').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(5000);
    expect(revision).toHaveBeenCalledTimes(2);
    await unmount(component);
    component = null;
    await vi.advanceTimersByTimeAsync(5000);
    expect(revision).toHaveBeenCalledTimes(2);
  });

  it('keeps failed input editable and exposes a retry without an empty iframe', async () => {
    open.mockRejectedValue(new Error('Test-owned unavailable sentinel'));
    component = mount(HtmlPreview, {
      target: document.body,
      props: { request: { source: '/missing.html' } },
    });
    await settle();
    expect(document.querySelector('[role="alert"]').textContent).toContain(
      'Test-owned unavailable sentinel',
    );
    expect(document.querySelector('iframe')).toBeNull();
    expect(document.querySelector('input').disabled).toBe(false);
    expect(document.querySelector('input').value).toBe('/missing.html');
  });

  it('retains a validated subpage on reload and ignores foreign frame messages', async () => {
    component = mount(HtmlPreview, {
      target: document.body,
      props: { request: { source: '/site/index.html' } },
    });
    await settle();
    const frame = document.querySelector('iframe');
    const reload = vi.spyOn(frame, 'src', 'set');
    const subpage = new URL(
      '/api/preview-assets/capability/sub/page.html#section',
      window.location.href,
    ).href;
    const notify = (url, source = frame.contentWindow) =>
      window.dispatchEvent(
        new MessageEvent('message', {
          origin: 'null',
          source,
          data: { type: 'vbot-preview-ready', url },
        }),
      );
    notify(subpage);
    notify('https://attacker.example/');
    notify(new URL('/api/rpc', window.location.href).href);
    notify(result.url, window);
    document.querySelector('button[aria-label="Reload preview"]').click();
    flushSync();
    expect(reload).toHaveBeenLastCalledWith(subpage);
    const label = document.querySelector('.html-preview__filename');
    expect(label.textContent).toBe('sub/page.html');
  });

  it('does not erase a failed open when the previous preview polls successfully', async () => {
    component = mount(HtmlPreview, {
      target: document.body,
      props: { request: { source: '/site/index.html' } },
    });
    await settle();
    const frame = document.querySelector('iframe');
    open.mockRejectedValue(new Error('Test-owned failed replacement'));
    const input = document.querySelector('input');
    input.value = '/missing.html';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    flushSync();
    const form = document.querySelector('form');
    const submit = new Event('submit', { bubbles: true, cancelable: true });
    form.dispatchEvent(submit);
    await settle();
    await vi.advanceTimersByTimeAsync(1500);
    expect(document.querySelector('iframe')).toBe(frame);
    expect(document.querySelector('[role="alert"]').textContent).toContain(
      'Test-owned failed replacement',
    );
  });

  it('shows localized feedback for a missing page and recovers on readiness', async () => {
    component = mount(HtmlPreview, {
      target: document.body,
      props: { request: { source: '/site/index.html' } },
    });
    await settle();
    const frame = document.querySelector('iframe');
    const notify = (type) => {
      window.dispatchEvent(
        new MessageEvent('message', {
          origin: 'null',
          source: frame.contentWindow,
          data: { type, url: new URL(result.url, window.location.href).href },
        }),
      );
      flushSync();
    };
    notify('vbot-preview-unavailable');
    expect(document.querySelector('[role="alert"]')).toBeTruthy();
    expect(frame.classList.contains('unavailable')).toBe(true);
    notify('vbot-preview-ready');
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect(frame.classList.contains('unavailable')).toBe(false);
  });
});
