<script>
  import ModelName from './ModelName.svelte';

  let { row, column } = $props();
  let id = $derived(row?.[column.id]);
</script>

{#if id}
  <ModelName {id} />
{:else}
  —
{/if}
