// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import HtmlPreview from '../HtmlPreview.svelte';
import { init, t } from '../../../lib/i18n.js';

const { open, revision } = vi.hoisted(() => ({
  open: vi.fn(),
  revision: vi.fn(),
}));
vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock(
  'svelte/store',
  async () =>
    import('../../../../node_modules/svelte/src/store/index-client.js'),
);
vi.mock('$lib/api.js', () => ({
  openFilePreview: open,
  getFilePreviewRevision: revision,
}));

const OUTPUT = { source: '/api/files/output-token' };
const result = {
  token: 'capability',
  url: '/api/preview-assets/capability/index.html',
  source: '/site/index.html',
  root: '/site',
  filename: 'index.html',
  revision: 'one',
};
const POLL_MS = 1500;
// The `preview.*` keys have no catalog entry yet, so assertions pass the
// fallback that the component shows.

let component;

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

// Mounts the preview for `request`; the returned `request` handle reopens a
// file by assigning `request.current`.
async function mountPreview(initialRequest = OUTPUT) {
  const request = fromStore(writable(initialRequest));
  component = mount(HtmlPreview, {
    target: document.body,
    props: {
      get request() {
        return request.current;
      },
    },
  });
  await settle();
  return { request, frame: document.querySelector('iframe') };
}

function frame() {
  return document.querySelector('iframe');
}

function assetUrl(path) {
  return new URL(path, window.location.href).href;
}

// Posts a preview-document message as the frame's window unless `source`
// says otherwise.
function postFromFrame(previewFrame, type, url, source) {
  window.dispatchEvent(
    new MessageEvent('message', {
      origin: 'null',
      source: source ?? previewFrame.contentWindow,
      data: { type, url },
    }),
  );
  flushSync();
}

function alertBanner() {
  return document.querySelector('[role="alert"]');
}

describe('HtmlPreview', () => {
  it('refreshes only on changes, pauses polling and cleans up on unmount', async () => {
    const { frame: first } = await mountPreview();
    expect(first.getAttribute('sandbox')).toBe('allow-scripts allow-downloads');
    expect(document.querySelector('input, form')).toBeNull();

    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(frame()).toBe(first);
    revision.mockResolvedValue({ revision: 'two' });
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await settle();
    expect(first.isConnected).toBe(false);
    expect(frame().getAttribute('src')).toBe(result.url);

    document.querySelector('[role="switch"]').click();
    flushSync();
    await vi.advanceTimersByTimeAsync(5000);
    expect(revision).toHaveBeenCalledTimes(2);
    await unmount(component);
    component = null;
    await vi.advanceTimersByTimeAsync(5000);
    expect(revision).toHaveBeenCalledTimes(2);
  });

  it('retries a failed Agent file output without asking for a path', async () => {
    open.mockRejectedValue(new Error('Test-owned unavailable sentinel'));
    await mountPreview({ source: '/api/files/missing-token' });

    expect(alertBanner().textContent).toContain(t('preview.failed'));
    expect(alertBanner().textContent).toContain(
      'Test-owned unavailable sentinel',
    );
    expect(frame()).toBeNull();
    expect(document.querySelector('input, form')).toBeNull();

    open.mockResolvedValue(result);
    alertBanner().querySelector('button').click();
    await settle();
    expect(open).toHaveBeenLastCalledWith(
      '/api/files/missing-token',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(frame()).not.toBeNull();
  });

  it('retains a validated subpage on reload and ignores foreign frame messages', async () => {
    const { frame: first } = await mountPreview();
    const subpage = assetUrl(
      '/api/preview-assets/capability/sub/page.html#section',
    );
    const originalWindow = first.contentWindow;
    const ready = (url, source = originalWindow) =>
      postFromFrame(first, 'vbot-preview-ready', url, source);
    ready(subpage);
    ready('https://attacker.example/');
    ready(assetUrl('/api/rpc'));
    ready(result.url, window);
    document
      .querySelector(`button[aria-label="${t('preview.reload')}"]`)
      .click();
    await settle();
    const replacement = frame();
    expect(replacement).not.toBe(first);
    expect(replacement.src).toBe(subpage);
    expect(document.querySelector('.html-preview__filename').textContent).toBe(
      'sub/page.html',
    );

    // A queued message from a retired document must not change the new target.
    ready(assetUrl(result.url));
    revision.mockResolvedValue({ revision: 'two' });
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await settle();
    expect(frame()).not.toBe(replacement);
    expect(frame().src).toBe(subpage);
  });

  it('navigates to the entry again when the same file is explicitly reopened', async () => {
    const { request, frame: first } = await mountPreview();
    postFromFrame(
      first,
      'vbot-preview-ready',
      assetUrl('/api/preview-assets/capability/sub.html'),
    );
    await settle();
    expect(document.querySelector('.html-preview__filename').textContent).toBe(
      'sub.html',
    );

    request.current = { ...OUTPUT };
    await settle();
    expect(first.isConnected).toBe(false);
    expect(frame().getAttribute('src')).toBe(result.url);
    expect(document.querySelector('.html-preview__filename').textContent).toBe(
      result.filename,
    );
  });

  it('shows feedback for a missing page and recovers on readiness', async () => {
    const { frame: first } = await mountPreview();
    const entry = assetUrl(result.url);

    postFromFrame(first, 'vbot-preview-unavailable', entry);
    expect(alertBanner()).not.toBeNull();
    expect(first.classList.contains('unavailable')).toBe(true);
    postFromFrame(first, 'vbot-preview-ready', entry);
    expect(alertBanner()).toBeNull();
    expect(first.classList.contains('unavailable')).toBe(false);
  });

  it('loads a fresh document when retrying an unavailable entry with Live paused', async () => {
    const { frame: first } = await mountPreview();
    document.querySelector('[role="switch"]').click();
    postFromFrame(first, 'vbot-preview-unavailable', assetUrl(result.url));
    await settle();

    alertBanner().querySelector('button').click();
    await settle();
    expect(alertBanner()).toBeNull();
    expect(first.isConnected).toBe(false);
    expect(frame().getAttribute('src')).toBe(result.url);
    expect(frame().classList.contains('unavailable')).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(revision).not.toHaveBeenCalled();
  });

  it('does not erase a failed open when the previous preview polls successfully', async () => {
    const { request, frame: first } = await mountPreview();
    open.mockRejectedValue(new Error('Test-owned failed replacement'));
    request.current = { source: '/api/files/missing-token' };
    await settle();
    await vi.advanceTimersByTimeAsync(POLL_MS);

    expect(frame()).toBe(first);
    expect(alertBanner().textContent).toContain(
      'Test-owned failed replacement',
    );
  });
});
