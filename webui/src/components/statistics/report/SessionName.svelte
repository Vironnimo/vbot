<script>
  import { tooltip } from '$lib/tooltip.js';
  import { sessionTooltip } from '$lib/statisticsView.js';

  let { row } = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the Session details here.) -->
<span
  class="stats-session"
  tabindex="0"
  class:stats-mono={!row.session_title}
  use:tooltip={sessionTooltip(row)}>{row.session_title || row.session_id}</span
>
