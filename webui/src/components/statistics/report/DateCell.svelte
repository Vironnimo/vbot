<script>
  import { activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatMoment } from '$lib/timeText.js';
  import { formatShortDateTime } from '$lib/statisticsView.js';

  let { row, column } = $props();
  let isoString = $derived(row?.[column.id]);
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the full moment here.) -->
<span
  class="stats-date"
  tabindex="0"
  use:tooltip={() => formatMoment(isoString)}
  >{formatShortDateTime(isoString, activeLocaleTag())}</span
>
