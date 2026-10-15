<script>
  import { activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatCost } from '$lib/statisticsView.js';

  let { amount, basis = '' } = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the cost basis here.) -->
<span class="stats-number" tabindex={basis ? 0 : undefined} use:tooltip={basis}
  >{formatCost(amount, activeLocaleTag())}</span
>
