<script>
  import { t } from '$lib/i18n.js';
  import { DAILY_GRANULARITIES } from '$lib/statisticsView.js';
  let { value: granularity = $bindable() } = $props();
  function granularityLabel(value) {
    switch (value) {
      case 'week':
        return t('statistics.granularity.week', 'Week');
      case 'month':
        return t('statistics.granularity.month', 'Month');
      default:
        return t('statistics.granularity.day', 'Day');
    }
  }
</script>

<div
  class="stats-toggle"
  role="group"
  aria-label={t('statistics.granularity.label', 'Period')}
>
  {#each DAILY_GRANULARITIES as value (value)}
    <button
      type="button"
      class="stats-toggle__option"
      class:stats-toggle__option--active={granularity === value}
      aria-pressed={granularity === value}
      onclick={() => (granularity = value)}
    >
      {granularityLabel(value)}
    </button>
  {/each}
</div>
