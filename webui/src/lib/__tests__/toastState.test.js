import { describe, expect, it } from 'vitest';

import { addToast, createToastState, dismissToast } from '../toastState.js';

describe('toast state helpers', () => {
  it('adds toasts with stable fields and normalizes unknown variants', () => {
    const state = createToastState();

    addToast(state, {
      title: 'Error',
      message: 'Provider failed',
      variant: 'error',
    });
    addToast(state, {
      title: 'Other',
      message: 'Uses fallback variant',
      variant: 'unexpected',
    });

    expect(state.toasts).toEqual([
      {
        id: expect.stringMatching(/^toast-\d+$/),
        title: 'Error',
        message: 'Provider failed',
        variant: 'error',
      },
      {
        id: expect.stringMatching(/^toast-\d+$/),
        title: 'Other',
        message: 'Uses fallback variant',
        variant: 'info',
      },
    ]);
    expect(state.toasts[0].id).not.toBe(state.toasts[1].id);
  });

  it('dismisses only the matching toast id', () => {
    const state = createToastState();
    addToast(state, { title: 'First', message: 'A', variant: 'info' });
    addToast(state, { title: 'Second', message: 'B', variant: 'warn' });
    const dismissedToastId = state.toasts[0].id;

    dismissToast(state, dismissedToastId);

    expect(state.toasts).toEqual([
      expect.objectContaining({ title: 'Second', message: 'B' }),
    ]);
  });
});
