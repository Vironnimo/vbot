// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const STORAGE_KEY = 'vbot.clientConnectionId';

describe('clientIdentity', () => {
  beforeEach(() => {
    // A fresh module each test resets the in-tab module cache so the
    // sessionStorage-driven paths are observable in isolation.
    vi.resetModules();
    sessionStorage.clear();
    window.history.replaceState({}, '', '/');
  });

  it('mints a non-empty id and persists it to sessionStorage', async () => {
    const { resolveClientConnectionId } = await import('../clientIdentity.js');

    const id = resolveClientConnectionId();

    expect(id).toBeTruthy();
    expect(sessionStorage.getItem(STORAGE_KEY)).toBe(id);
  });

  it('returns the same id across calls within the tab', async () => {
    const { resolveClientConnectionId } = await import('../clientIdentity.js');

    expect(resolveClientConnectionId()).toBe(resolveClientConnectionId());
  });

  it('reuses an id already stored for the tab', async () => {
    sessionStorage.setItem(STORAGE_KEY, 'tab-seed');
    const { resolveClientConnectionId } = await import('../clientIdentity.js');

    expect(resolveClientConnectionId()).toBe('tab-seed');
  });

  it('does not read localStorage (per-tab id, not per-browser)', async () => {
    localStorage.setItem(STORAGE_KEY, 'shared-across-tabs');
    const { resolveClientConnectionId } = await import('../clientIdentity.js');

    expect(resolveClientConnectionId()).not.toBe('shared-across-tabs');
  });

  it('reports the browser accessor by default', async () => {
    const { resolveAccessorType } = await import('../clientIdentity.js');

    expect(resolveAccessorType()).toBe('browser');
  });

  it('reports the desktop accessor when loaded via the desktop URL', async () => {
    window.history.replaceState({}, '', '/?accessor=desktop');
    const { resolveAccessorType } = await import('../clientIdentity.js');

    expect(resolveAccessorType()).toBe('desktop');
  });
});
