<script>
  import { tooltip as tooltipAction } from '$lib/tooltip.js';

  // The `marker` option decoration (lib/dropdownPanel.js::optionDecorations):
  // a quiet icon right after an option label. `label` is its accessible name;
  // `tooltip` (quick-tooltip content) explains it on hover.
  let { label, tooltip = '' } = $props();
</script>

<span
  class="option-marker"
  role="img"
  aria-label={label}
  use:tooltipAction={tooltip || label}
>
  <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
    <circle
      cx="6"
      cy="6"
      r="4.75"
      fill="none"
      stroke="currentColor"
      stroke-width="1.25"
      stroke-dasharray="2.2 1.6"
    />
  </svg>
</span>

<style>
  .option-marker {
    display: inline-flex;
    flex-shrink: 0;
    padding: 2px;
    color: var(--text-lo);
  }
</style>
