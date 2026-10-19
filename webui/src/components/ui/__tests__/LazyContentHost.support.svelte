<script>
  import { setContext } from 'svelte';
  import LazyContent from '../LazyContent.svelte';

  let { load, showFeedback = true } = $props();
  setContext('lazy-content-test', 'context survives the lazy boundary');

  export function changeLoader(next) {
    load = next;
  }

  export function setFeedback(visible) {
    showFeedback = visible;
  }
</script>

<LazyContent {load} {showFeedback}>
  {#snippet children(Component)}<Component />{/snippet}
</LazyContent>
