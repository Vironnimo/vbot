<script>
  import { tooltip as tooltipAction } from '$lib/tooltip.js';

  // The `marker` option decoration (lib/dropdownPanel.js::optionDecorations):
  // a quiet check right after an option label. `label` is its accessible name;
  // `tooltip` (quick-tooltip content, the label placed beside the list) shows
  // it on hover.
  let { label, tooltip = '' } = $props();
</script>

<span
  class="option-marker"
  role="img"
  aria-label={label}
  use:tooltipAction={tooltip || label}
>
  <svg viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
    <path
      d="M2.75 6.25 5 8.5l4.25-4.75"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
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
