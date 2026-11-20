<script>
  import Button from '../ui/Button.svelte';
  import DataTable from '../ui/DataTable.svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatMoment } from '$lib/timeText.js';
  import {
    providerUsage,
    watchProviderUsage,
  } from '$lib/providerUsage.svelte.js';
  import {
    clampUsagePercent,
    formatResetAt,
    usageComparison,
    usageSeverity,
  } from '$lib/statisticsLimits.js';

  let { onOpenLimits = () => {} } = $props();
  const locale = $derived(activeLocaleTag());
  const comparison = $derived(
    usageComparison(providerUsage.report?.providers ?? [], locale),
  );

  function periodLabel(id) {
    if (id === '5h') return t('statistics.overview.limitsFiveHours');
    if (id === 'Week') return t('statistics.granularity.week');
    if (id === 'Month') return t('statistics.granularity.month');
    if (id === 'Day') return t('statistics.granularity.day');
    return id;
  }

  const columns = $derived([
    {
      id: 'provider',
      label: t('statistics.dimension.provider'),
      sortable: false,
      cell: providerCell,
      width: 'var(--provider-width)',
    },
    ...comparison.periods.map(({ id }) => ({
      id: `period:${id}`,
      period: id,
      label: periodLabel(id),
      sortable: false,
      cell: periodCell,
    })),
  ]);

  $effect(() => watchProviderUsage());
</script>

{#snippet providerCell({ snapshot, credits, byPeriod })}
  <div class="provider-name">{snapshot.display_name}</div>
  {@const account = snapshot.account !== 'default' && snapshot.account}
  {#if account || snapshot.plan || (credits && byPeriod.size > 0)}
    <div class="provider-meta">
      {[account, snapshot.plan, byPeriod.size > 0 && credits]
        .filter(Boolean)
        .join(' · ')}
    </div>
  {/if}
{/snippet}

{#snippet periodCell(row, column)}
  {@const windows = row.byPeriod.get(column.period)}
  {#if windows}
    <div class="cell-period">{column.label}</div>
    {#each windows as window, index (index)}
      {@const percent = clampUsagePercent(window.used_percent)}
      {@const reset = formatResetAt(window.reset_at, locale)}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Focus reveals the exact reset time.) -->
      <div
        class="usage-window"
        role="meter"
        aria-label={`${row.snapshot.display_name} · ${window.label}`}
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={Math.round(percent)}
        tabindex={reset ? 0 : undefined}
        use:tooltip={() =>
          reset
            ? {
                title: t('statistics.limits.resets'),
                text: formatMoment(window.reset_at),
              }
            : ''}
      >
        {#if windows.length > 1}
          <span class="window-label">{window.label}</span>
        {/if}
        <div class="reading">
          <span class="track">
            <span
              class={`fill fill--${usageSeverity(percent)}`}
              style:width={`${percent}%`}
            ></span>
          </span>
          <span class="used"
            >{t('statistics.overview.limitsPercent', {
              percent: Math.round(percent),
            })}</span
          >
        </div>
        {#if reset}
          <span class="reset">
            {reset.relative
              ? t('statistics.limits.resetsIn', { duration: reset.relative })
              : reset.absolute}
          </span>
        {/if}
      </div>
    {/each}
  {:else}
    <span class="missing">—</span>
  {/if}
{/snippet}

{#if comparison.rows.length || comparison.balances.length}
  <section
    class="stats-limits-strip"
    aria-labelledby="stats-limits-strip-title"
  >
    <div class="strip-head">
      <h3 id="stats-limits-strip-title">
        {t('statistics.overview.limitsTitle')}
      </h3>
      <Button
        variant="tertiary"
        class="stats-block__link"
        onClick={onOpenLimits}
      >
        {t('statistics.overview.limitsAll')}
        <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"
          ><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></svg
        >
      </Button>
    </div>
    {#if comparison.rows.length}
      <div class="matrix" style:--periods={comparison.periods.length}>
        <DataTable
          {columns}
          rows={comparison.rows}
          rowKey={(row) => row.key}
          ariaLabel={t('statistics.overview.limitsTitle')}
          dense
        />
      </div>
    {/if}
    {#if comparison.balances.length}
      <ul class="balances">
        {#each comparison.balances as row (row.key)}
          <li>
            <div>{@render providerCell(row)}</div>
            <span>{row.credits}</span>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{/if}

<style>
  .stats-limits-strip {
    --provider-width: 23%;
    min-width: 0;
    padding: var(--space-md) var(--space-lg);
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }
  .strip-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
    margin-bottom: var(--space-xs);
  }
  .strip-head h3 {
    margin: 0;
    color: var(--text-hi);
    font-size: var(--fs-label-md);
    font-weight: 600;
  }
  .matrix :global(.data-table__table) {
    table-layout: fixed;
    min-width: calc(8rem + var(--periods) * 7rem);
  }
  .matrix :global(.data-table__cell:first-child) {
    padding-inline-start: 0;
  }
  .matrix :global(td.data-table__cell) {
    vertical-align: middle;
  }
  .matrix :global(th.data-table__cell) {
    white-space: normal;
    overflow-wrap: anywhere;
  }
  .provider-name {
    font-weight: 600;
    font-size: var(--fs-body-sm);
    color: var(--text-hi);
    overflow-wrap: anywhere;
  }
  .provider-meta,
  .reset,
  .window-label,
  .cell-period,
  .missing {
    color: var(--text-lo);
    font-size: var(--fs-label-sm);
    overflow-wrap: anywhere;
  }
  .provider-meta,
  .reset {
    display: block;
    margin-top: 2px;
  }
  .cell-period {
    display: none;
  }
  .usage-window + .usage-window {
    margin-top: var(--space-xs);
  }
  .reading {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 4ch;
    align-items: center;
    gap: var(--space-sm);
    border-radius: var(--r-sm);
  }
  .usage-window:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .track {
    height: 5px;
    overflow: hidden;
    border-radius: var(--r-sm);
    background: var(--surface-3);
  }
  .fill {
    display: block;
    height: 100%;
    background: var(--blue);
  }
  .fill--warn {
    background: var(--amber);
  }
  .fill--critical {
    background: var(--red);
  }
  .used {
    color: var(--text-hi);
    font-size: var(--fs-label-sm);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    text-align: end;
  }
  .balances {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .balances li {
    display: grid;
    grid-template-columns: var(--provider-width) minmax(0, 1fr);
    align-items: center;
    border-top: 1px solid var(--border);
    padding: var(--space-sm) 0;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
    font-variant-numeric: tabular-nums;
  }
  .balances li > span {
    padding-inline-start: 10px;
  }
  .balances li:last-child {
    padding-bottom: 0;
  }
  @container stats-view (max-width: 600px) {
    .stats-limits-strip {
      padding: var(--space-sm) var(--space-md);
    }
    .matrix :global(.data-table__table) {
      display: block;
      min-width: 0;
    }
    .matrix :global(thead) {
      position: absolute;
      clip-path: inset(50%);
      width: 1px;
      height: 1px;
      overflow: hidden;
    }
    .matrix :global(tbody) {
      display: block;
    }
    .matrix :global(.data-table__row) {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(100%, 5.75rem), 1fr));
      gap: var(--space-xs) var(--space-md);
      border-top: 1px solid var(--border);
      padding: var(--space-sm) 0;
    }
    .matrix :global(td.data-table__cell) {
      padding: 0;
      border: 0;
      min-width: 0;
    }
    .matrix :global(td:first-child) {
      grid-column: 1/-1;
    }
    .matrix :global(td):has(> .missing) {
      display: none;
    }
    .cell-period {
      display: block;
      margin-bottom: 2px;
      color: var(--text-med);
    }
    .balances li {
      grid-template-columns: auto 1fr;
      gap: var(--space-md);
    }
    .balances li > span {
      padding: 0;
    }
  }
</style>
