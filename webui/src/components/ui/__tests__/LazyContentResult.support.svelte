<script>
  import { getContext } from 'svelte';
  const message = getContext('lazy-content-test');
</script>

<p data-testid="loaded-content">{message}</p>
