<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import {
    createComputers,
    provideComputers,
  } from '../../lib/computers.svelte.js';

  let {
    component: Component,
    componentProps = {},
    readComputers,
    onCatalog = () => {},
  } = $props();
  const catalog = provideComputers(
    createComputers({ read: (...args) => readComputers(...args) }),
  );
  untrack(() => onCatalog(catalog));
  onMount(() => void catalog.refresh());
  onDestroy(() => catalog.destroy());
</script>

<Component {...componentProps} />
