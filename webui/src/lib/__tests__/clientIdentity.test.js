// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const STORAGE_KEY = 'vbot.clientConnectionId';

// A fresh module per test resets the module-level id cache.
const loadClientIdentity = () => import('../clientIdentity.js');

describe('clientIdentity', () => {
  beforeEach(() => {
    vi.resetModules();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('mints one id for the tab and keeps it in sessionStorage', async () => {
    const { resolveClientConnectionId } = await loadClientIdentity();

    const id = resolveClientConnectionId();

    expect(id).toBeTruthy();
    expect(resolveClientConnectionId()).toBe(id);
    expect(sessionStorage.getItem(STORAGE_KEY)).toBe(id);
  });

  it('reuses the id already stored for the tab', async () => {
    sessionStorage.setItem(STORAGE_KEY, 'tab-seed');
    const { resolveClientConnectionId } = await loadClientIdentity();

    expect(resolveClientConnectionId()).toBe('tab-seed');
  });

  it('ignores an id in localStorage, which every tab shares', async () => {
    localStorage.setItem(STORAGE_KEY, 'shared-across-tabs');
    const { resolveClientConnectionId } = await loadClientIdentity();

    expect(resolveClientConnectionId()).not.toBe('shared-across-tabs');
  });

  it.each([
    ['/', 'browser'],
    ['/?accessor=desktop', 'desktop'],
  ])(
    'reports the accessor of a window loaded at %s as %s',
    async (url, type) => {
      window.history.replaceState({}, '', url);
      const { resolveAccessorType } = await loadClientIdentity();

      expect(resolveAccessorType()).toBe(type);
    },
  );
});
