<script>
  import { tooltip } from '$lib/tooltip.js';
  import ModelId from '../../ui/ModelId.svelte';

  let { id } = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach a truncated Model id here.) -->
<span
  class="stats-id"
  tabindex="0"
  use:tooltip={{ text: id, whenTruncated: true }}><ModelId {id} /></span
>
