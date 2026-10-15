<script>
  import { activeLocaleTag } from '$lib/i18n.js';
  import { formatInteger } from '$lib/statisticsView.js';

  let { entries, label, valueText = null, ariaLabel = '' } = $props();
</script>

<ul class="stats-bars" aria-label={ariaLabel || undefined}>
  {#each entries as item, index (index)}
    <li class="stats-bars__row">
      <span class="stats-bars__label">{@render label(item.entry)}</span>
      <span class="stats-bars__track" aria-hidden="true">
        <span
          class="stats-bars__fill"
          style:width={`${Math.round(item.fraction * 100)}%`}
        ></span>
      </span>
      <span class="stats-bars__value"
        >{valueText
          ? valueText(item.entry)
          : formatInteger(item.value, activeLocaleTag())}</span
      >
    </li>
  {/each}
</ul>
