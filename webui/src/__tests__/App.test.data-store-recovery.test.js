import { describe, expect, it } from 'vitest';

import { createAppControllerState } from '../lib/appController.js';

describe('Data-store recovery surface', () => {
  it('keeps data-store health and incident state in the global app projection', () => {
    const state = createAppControllerState('chat');

    expect(state.dataStoreHealth).toBeNull();
    expect(state.dataStoreIncident).toBeNull();
  });
});
