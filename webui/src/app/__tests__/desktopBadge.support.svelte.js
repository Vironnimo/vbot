import { createDesktopBadge } from '../desktopBadge.svelte.js';

// The badge owner in an effect root, with counts the test can change.
export function createBadgeHarness({ attended = 0, unattended = 0 } = {}) {
  const counts = $state({ supported: true, attended, unattended });
  const dispose = $effect.root(() => {
    createDesktopBadge({
      get supported() {
        return counts.supported;
      },
      get attendedCount() {
        return counts.attended;
      },
      get unattendedCount() {
        return counts.unattended;
      },
    });
  });
  return { counts, dispose };
}
