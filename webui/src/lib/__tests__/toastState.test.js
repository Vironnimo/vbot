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

  it('updates a keyed toast in place, including its action, without merging other owners', () => {
    const state = createToastState();
    const key = JSON.stringify(['extension', 'factory', 'one']);
    const id = addToast(state, { title: 'Old', key });
    addToast(state, {
      title: 'Other',
      key: JSON.stringify(['extension', 'other', 'one']),
    });
    const action = { label: 'Open', onClick: () => {} };
    expect(addToast(state, { title: 'New', key, actions: [action] })).toBe(id);
    expect(state.toasts).toHaveLength(2);
    expect(state.toasts[0]).toMatchObject({
      id,
      title: 'New',
      actions: [action],
    });
  });
});
