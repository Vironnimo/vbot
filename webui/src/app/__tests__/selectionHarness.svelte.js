import { createAppSelection } from '../selection.svelte.js';

export function createSelectionHarness() {
  let selection;
  const dispose = $effect.root(() => {
    selection = createAppSelection({});
  });
  return { selection, dispose };
}
