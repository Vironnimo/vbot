<script>
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import InfoHint from '../../ui/InfoHint.svelte';

  let { tile } = $props();
</script>

<div class="stats-tile">
  <span class="stats-tile__label">
    <span>{tile.label}</span>
    {#if tile.hint}
      <InfoHint text={tile.hint} />
    {/if}
    {#if tile.warning}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the warning text here.) -->
      <span
        class="stats-tile__warning"
        role="img"
        aria-label={tile.warning}
        tabindex="0"
        use:tooltip={tile.warning}
        ><svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"
          ><path d="M12 3 2 21h20L12 3Z" /><path d="M12 10v5" /><path
            d="M12 18h.01"
          /></svg
        ></span
      >
    {/if}
  </span>
  <span class="stats-tile__value-row">
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the exact value here.) -->
    <span
      class="stats-tile__value"
      class:stats-tile__value--more={Boolean(tile.valueTooltip)}
      tabindex={tile.valueTooltip ? 0 : undefined}
      use:tooltip={tile.valueTooltip ?? ''}>{tile.value}</span
    >
    {#if tile.change}
      {@render changeMarker(tile.change)}
    {/if}
  </span>
  {#if tile.detail}
    <span class="stats-tile__detail-row">
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the breakdown here.) -->
      <span
        class="stats-tile__detail"
        class:stats-tile__detail--more={Boolean(tile.detailTooltip)}
        tabindex={tile.detailTooltip ? 0 : undefined}
        use:tooltip={tile.detailTooltip ?? ''}>{tile.detail}</span
      >
      {#if tile.detailChange}
        {@render changeMarker(tile.detailChange)}
      {/if}
    </span>
  {/if}
</div>

{#snippet changeMarker(change)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the previous value here.) -->
  <span
    class={[
      'stats-change',
      `stats-change--${change.direction}`,
      `stats-change--${change.tone}`,
    ]}
    tabindex={change.tooltip ? 0 : undefined}
    use:tooltip={change.tooltip ?? ''}
  >
    <span aria-hidden="true"
      >{change.direction === 'up'
        ? '▲'
        : change.direction === 'down'
          ? '▼'
          : '•'}</span
    >
    <span class="stats-sr-only"
      >{change.direction === 'up'
        ? t('statistics.change.up')
        : change.direction === 'down'
          ? t('statistics.change.down')
          : t('statistics.change.flat')}</span
    >
    {change.text}
  </span>
{/snippet}
