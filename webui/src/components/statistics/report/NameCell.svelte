<script>
  import { tooltip } from '$lib/tooltip.js';

  let { row, column } = $props();
  let name = $derived(row?.[column.id]);
</script>

{#if name}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach a truncated name here.) -->
  <span
    class="stats-id"
    tabindex="0"
    use:tooltip={{ text: name, whenTruncated: true }}>{name}</span
  >
{:else}
  —
{/if}
