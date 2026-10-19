// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { loadComponent } from '../../../lib/lazyContent.js';

vi.mock('svelte', async () => {
  return import('../../../../node_modules/svelte/src/index-client.js');
});

const { default: Host } = await import('./LazyContentHost.support.svelte');
const { default: Result } = await import('./LazyContentResult.support.svelte');
let mounted;

afterEach(async () => {
  if (mounted) await unmount(mounted);
  mounted = null;
  document.body.innerHTML = '';
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function render(load) {
  mounted = mount(Host, { target: document.body, props: { load } });
  flushSync();
}

async function settle(load) {
  await Promise.resolve(loadComponent(load)).catch(() => {});
  await tick();
  flushSync();
}

describe('LazyContent', () => {
  it('shares pending imports, preserves context and renders cached revisits immediately', async () => {
    const request = deferred();
    const load = vi.fn(() => request.promise);
    render(load);
    const pending = loadComponent(load);
    expect(loadComponent(load)).toBe(pending);
    expect(
      document.querySelector('[role="status"]').getAttribute('aria-busy'),
    ).toBe('true');

    request.resolve({ default: Result });
    await settle(load);
    expect(load).toHaveBeenCalledTimes(1);
    expect(
      document.querySelector('[data-testid="loaded-content"]').textContent,
    ).toBe('context survives the lazy boundary');
    expect(document.querySelector('[role="status"]')).toBeNull();
    expect(loadComponent(load)).toBe(Result);

    const content = document.querySelector('[data-testid="loaded-content"]');
    mounted.setFeedback(false);
    flushSync();
    expect(document.querySelector('[data-testid="loaded-content"]')).toBe(
      content,
    );

    await unmount(mounted);
    render(load);
    expect(
      document.querySelector('[data-testid="loaded-content"]'),
    ).not.toBeNull();
    expect(document.querySelector('[role="status"]')).toBeNull();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('offers a document reload after a failed import, only when its view is visible', async () => {
    const request = deferred();
    const load = vi.fn(() => request.promise);
    render(load);
    mounted.setFeedback(false);
    flushSync();
    expect(document.querySelector('[role="status"]')).toBeNull();
    const pending = loadComponent(load);
    request.reject(new Error('network unavailable'));
    await expect(pending).rejects.toThrow('network unavailable');
    await tick();
    flushSync();
    expect(document.querySelector('[role="alert"]')).toBeNull();
    mounted.setFeedback(true);
    flushSync();
    expect(document.querySelector('[role="alert"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="loaded-content"]')).toBeNull();
    expect(
      Array.from(document.querySelectorAll('button'), (button) =>
        button.textContent.trim(),
      ),
    ).toEqual(['Reload page']);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('ignores a previous view resolving after navigation changes the loader', async () => {
    const first = deferred();
    const second = deferred();
    const loadFirst = () => first.promise;
    const loadSecond = () => second.promise;
    render(loadFirst);
    mounted.changeLoader(loadSecond);
    flushSync();

    first.resolve({ default: Result });
    await settle(loadFirst);
    expect(document.querySelector('[data-testid="loaded-content"]')).toBeNull();
    expect(document.querySelector('[role="status"]')).not.toBeNull();

    second.resolve({ default: Result });
    await settle(loadSecond);
    expect(
      document.querySelector('[data-testid="loaded-content"]'),
    ).not.toBeNull();
    expect(document.querySelector('[role="status"]')).toBeNull();
  });
});
