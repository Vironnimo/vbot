import { describe, expect, it } from 'vitest';

import {
  RESOURCE_TOKEN_MODELS,
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

  it('returns no tokens for an unknown or out-of-scope kind', () => {
    expect(tokenKeysForKind('queue')).toEqual([]);
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
