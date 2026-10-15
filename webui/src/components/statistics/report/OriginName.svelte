<script>
  import { originLabel } from '$lib/statisticsView.js';

  let { origin } = $props();
</script>

<span class="stats-origin-name">
  <span class={['stats-swatch', `stats-swatch--${origin}`]} aria-hidden="true"
  ></span>
  {originLabel(origin)}
</span>
