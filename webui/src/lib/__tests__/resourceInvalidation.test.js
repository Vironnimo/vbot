import { describe, expect, it } from 'vitest';

import {
  RESOURCE_TOKEN_AGENTS,
  RESOURCE_TOKEN_MODELS,
  RESOURCE_TOKEN_SESSIONS,
  SURFACE_DISPLAY,
  SURFACE_FORM,
  isSurfaceBusy,
  shouldApplyReloadNow,
  tokenKeysForKind,
} from '../resourceInvalidation.js';

describe('tokenKeysForKind()', () => {
  it('routes a model-catalog change to the models token', () => {
    expect(tokenKeysForKind('models')).toEqual([RESOURCE_TOKEN_MODELS]);
  });

  it('routes a provider change to the models token (availability changed)', () => {
    expect(tokenKeysForKind('providers')).toEqual([RESOURCE_TOKEN_MODELS]);
  });

  it('routes an agents change to the agents token', () => {
    expect(tokenKeysForKind('agents')).toEqual([RESOURCE_TOKEN_AGENTS]);
  });

  it('routes a sessions change to the sessions token', () => {
    expect(tokenKeysForKind('sessions')).toEqual([RESOURCE_TOKEN_SESSIONS]);
  });

  it('returns no tokens for the queue kind (scope-routed, not token-routed)', () => {
    // `queue` carries a session scope the watcher must match, so App routes it
    // directly rather than through a counter — it has no token group.
    expect(tokenKeysForKind('queue')).toEqual([]);
  });

  it('returns no tokens for an unknown kind', () => {
    expect(tokenKeysForKind('clients')).toEqual([]);
    expect(tokenKeysForKind(undefined)).toEqual([]);
  });
});

describe('isSurfaceBusy()', () => {
  it('is idle with no signals', () => {
    expect(isSurfaceBusy()).toBe(false);
    expect(isSurfaceBusy({})).toBe(false);
  });

  it('is busy while a dropdown is open', () => {
    expect(isSurfaceBusy({ dropdownOpen: true })).toBe(true);
  });

  it('is busy while a field holds focus', () => {
    expect(isSurfaceBusy({ focused: true })).toBe(true);
  });

  it('is busy while a debounced save is pending', () => {
    expect(isSurfaceBusy({ savePending: true })).toBe(true);
  });
});

describe('shouldApplyReloadNow()', () => {
  it('always applies immediately for a pure display', () => {
    expect(shouldApplyReloadNow(SURFACE_DISPLAY)).toBe(true);
    expect(shouldApplyReloadNow(SURFACE_DISPLAY, { dropdownOpen: true })).toBe(
      true,
    );
  });

  it('applies a form reload when the form is idle', () => {
    expect(shouldApplyReloadNow(SURFACE_FORM)).toBe(true);
    expect(shouldApplyReloadNow(SURFACE_FORM, {})).toBe(true);
  });

  it('defers a form reload while it is actively edited', () => {
    expect(shouldApplyReloadNow(SURFACE_FORM, { dropdownOpen: true })).toBe(
      false,
    );
    expect(shouldApplyReloadNow(SURFACE_FORM, { focused: true })).toBe(false);
    expect(shouldApplyReloadNow(SURFACE_FORM, { savePending: true })).toBe(
      false,
    );
  });
});
