<script>
  import { untrack } from 'svelte';
  import {
    createContributions,
    provideContributions,
  } from '../../lib/contributions.svelte.js';
  let {
    component: Component,
    instances = [{}],
    source = {},
    onRegistry = () => {},
  } = $props();
  const registry = provideContributions(
    createContributions(untrack(() => source)),
  );
  untrack(() => onRegistry(registry));
</script>

{#each instances as componentProps, index (index)}
  <Component {...componentProps} />
{/each}
