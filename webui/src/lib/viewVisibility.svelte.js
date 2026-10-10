import { onMount } from 'svelte';

// Retained editors stay mounted for their drafts. Status readers need both
// their owner's displayed-page state and the browser document's visibility.
export function createViewVisibility(getActive = () => true) {
  let documentVisible = $state(false);
  onMount(() => {
    const update = () => (documentVisible = !document.hidden);
    update();
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  });
  return {
    get visible() {
      return getActive() && documentVisible;
    },
  };
}
