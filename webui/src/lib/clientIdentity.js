// Per-window/per-tab client identity for the presence roster ("who is
// connected"). The WebSocket connect sends this id + accessor type as query
// params; the General settings panel matches the same id to mark "this window".
//
// The id is deliberately NOT in localStorage: localStorage is shared across a
// browser's tabs, which would collapse N tabs into one presence row, delete the
// others' entry when one tab closes, and mark "this window" everywhere. We use
// sessionStorage (per-tab, survives a reload within the tab) as the source of
// truth, with a module-level cache as the only fallback when sessionStorage is
// unavailable — so "N tabs = N rows" and the self-mark stays stable.

import { isDesktopAccessor } from './desktopBridge.js';

const CLIENT_CONNECTION_ID_STORAGE_KEY = 'vbot.clientConnectionId';

export const ACCESSOR_BROWSER = 'browser';
export const ACCESSOR_DESKTOP = 'desktop';

let cachedConnectionId = '';

// Return this tab's stable connection id, minting it once on first use.
export function resolveClientConnectionId() {
  const stored = readStoredConnectionId();
  if (stored) {
    cachedConnectionId = stored;
    return stored;
  }
  if (cachedConnectionId) {
    return cachedConnectionId;
  }
  cachedConnectionId = mintConnectionId();
  writeStoredConnectionId(cachedConnectionId);
  return cachedConnectionId;
}

// Which app surface this window is — Desktop shell vs. plain browser.
export function resolveAccessorType() {
  return isDesktopAccessor() ? ACCESSOR_DESKTOP : ACCESSOR_BROWSER;
}

function readStoredConnectionId() {
  try {
    if (typeof sessionStorage === 'undefined') {
      return '';
    }
    return sessionStorage.getItem(CLIENT_CONNECTION_ID_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function writeStoredConnectionId(id) {
  try {
    if (typeof sessionStorage !== 'undefined') {
      sessionStorage.setItem(CLIENT_CONNECTION_ID_STORAGE_KEY, id);
    }
  } catch {
    // sessionStorage unavailable (private mode / SSR) — the module cache holds it.
  }
}

function mintConnectionId() {
  try {
    if (typeof globalThis.crypto?.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
  } catch {
    // crypto unavailable — fall through to the time + random fallback.
  }
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
