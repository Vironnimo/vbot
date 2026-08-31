import { describe, expect, it } from 'vitest';

import { createAppControllerState } from '../lib/appController.js';

describe('Session-store recovery surface', () => {
  it('keeps Session-store health and incident state in the global app projection', () => {
    const state = createAppControllerState('chat');

    expect(state.sessionStoreHealth).toBeNull();
    expect(state.sessionStoreIncident).toBeNull();
  });
});
