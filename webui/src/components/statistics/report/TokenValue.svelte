<script>
  import { activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatTokens, tokenTooltip } from '$lib/statisticsView.js';

  let { value } = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the exact count here.) -->
<span
  class="stats-number"
  tabindex="0"
  use:tooltip={() => tokenTooltip(value, activeLocaleTag())}
  >{formatTokens(value, activeLocaleTag())}</span
>
