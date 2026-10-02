<script>
  // Shared data table. It owns sortable column headers (a real button per
  // sortable column, `aria-sort` on its header cell), an optional text filter
  // with an "N of M" count, an optional row limit with a Show all / Show fewer
  // toggle, optionally activatable rows, and the keyboard-scrollable
  // horizontal region. Callers pass already-translated column labels, the
  // rows and a stable row key; the pure rules (sorting, filtering, limiting,
  // default cell text) live in lib/dataTable.js.
  //
  // A column is `{ id, label, align?, sortable?, sortValue?,
  // defaultDirection?, format?, cell?, hint?, width?, mono? }`: `align: 'end'`
  // right-aligns numbers and sorts them descending first; `format(row)`
  // returns the cell text; a `cell` snippet `(row, column)` renders custom
  // markup instead; `hint` adds an InfoHint to the header.
  import { activeLocaleTag, t } from '../../lib/i18n.js';
  import {
    ariaSort,
    cellText,
    columnAlign,
    defaultDirection,
    formatCount,
    isSortable,
    nextSort,
    projectRows,
  } from '../../lib/dataTable.js';
  import Button from './Button.svelte';
  import EmptyState from './EmptyState.svelte';
  import InfoHint from './InfoHint.svelte';
  import TextField from './TextField.svelte';

  const generatedId = $props.id();

  let {
    columns = [],
    rows = [],
    rowKey,
    ariaLabel,
    sort = $bindable(),
    initialSort = null,
    limit = 0,
    filter = null,
    emptyText = '',
    dense = false,
    maxHeight = '',
    onRowClick = null,
    footer,
    class: className = '',
  } = $props();

  let query = $state('');
  let expanded = $state(false);

  const tableId = `${generatedId}-table`;
  const locale = $derived(activeLocaleTag());

  let activeSort = $derived(sort ?? initialSort ?? null);
  let filtering = $derived(Boolean(filter) && query.trim() !== '');
  let view = $derived(
    projectRows({
      rows,
      columns,
      sort: activeSort,
      query: filter ? query : '',
      filterText: filter?.text,
      limit,
      expanded,
      locale,
    }),
  );
  let rootClass = $derived(
    [
      'data-table',
      dense ? 'data-table--dense' : '',
      maxHeight ? 'data-table--bounded' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );

  function keyOf(row, index) {
    return rowKey ? rowKey(row) : index;
  }

  // The direction the sort indicator shows: the active direction, or for
  // another column the direction a click would choose.
  function indicatorDirection(column) {
    return activeSort?.column === column.id
      ? activeSort.direction
      : defaultDirection(column);
  }

  function chooseSort(column) {
    sort = nextSort(activeSort, column);
  }

  // Clicks on a control inside the row (a link, a button) and clicks that end
  // a text selection belong to that control or selection, not the row.
  const ROW_CONTROLS =
    'a[href], button, input, select, textarea, summary, label, [role="button"], [role="link"]';

  function handleRowClick(event, row) {
    const element = event.currentTarget;
    const control = event.target.closest?.(ROW_CONTROLS);
    if (control && control !== element && element.contains(control)) return;
    const selection = window.getSelection?.();
    if (
      selection &&
      !selection.isCollapsed &&
      element.contains(selection.anchorNode)
    )
      return;
    onRowClick(row);
  }

  function handleRowKeydown(event, row) {
    if (event.target !== event.currentTarget) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onRowClick(row);
    }
  }
</script>

<div class={rootClass} style:--data-table-max-height={maxHeight || undefined}>
  {#if view.total === 0}
    <EmptyState
      density="compact"
      description={emptyText || t('common.dataTable.empty')}
    />
  {:else}
    {#if filter}
      <div class="data-table__toolbar">
        <TextField
          type="search"
          class="data-table__filter"
          value={query}
          placeholder={filter.placeholder ||
            t('common.dataTable.filterPlaceholder')}
          ariaLabel={filter.label ||
            filter.placeholder ||
            t('common.dataTable.filterLabel')}
          autocomplete="off"
          onInput={(next) => (query = next)}
        />
        <span class="data-table__count" role="status">
          {#if filtering}
            {t('common.dataTable.matchCount', {
              count: formatCount(view.matched, locale),
              total: formatCount(view.total, locale),
            })}
          {/if}
        </span>
      </div>
    {/if}
    {#if view.matched === 0}
      <EmptyState
        density="compact"
        description={t('common.dataTable.noMatches')}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="data-table__scroll"
        role="region"
        tabindex="0"
        aria-label={ariaLabel}
      >
        <table id={tableId} class="data-table__table" aria-label={ariaLabel}>
          <thead>
            <tr>
              {#each columns as column (column.id)}
                <th
                  scope="col"
                  class={[
                    'data-table__cell',
                    columnAlign(column) === 'end' && 'data-table__cell--end',
                  ]}
                  style:width={column.width}
                  aria-sort={ariaSort(activeSort, column)}
                >
                  <span class="data-table__head">
                    {#if isSortable(column)}
                      <button
                        type="button"
                        class={[
                          'data-table__sort',
                          activeSort?.column === column.id &&
                            'data-table__sort--active',
                          indicatorDirection(column) === 'asc' &&
                            'data-table__sort--asc',
                        ]}
                        onclick={() => chooseSort(column)}
                      >
                        <span>{column.label}</span>
                        <svg
                          class="data-table__sort-icon"
                          viewBox="0 0 24 24"
                          width="12"
                          height="12"
                          aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg
                        >
                      </button>
                    {:else}
                      <span>{column.label}</span>
                    {/if}
                    {#if column.hint}
                      <InfoHint
                        class="data-table__hint"
                        text={column.hint}
                        ariaLabel={t('common.dataTable.columnHint', {
                          column: column.label,
                        })}
                      />
                    {/if}
                  </span>
                </th>
              {/each}
            </tr>
          </thead>
          <tbody>
            {#each view.rows as row, index (keyOf(row, index))}
              {#if onRowClick}
                <!-- An activatable row stays a table row (no button role), so
                     its cells keep their column headers. -->
                <tr
                  class="data-table__row data-table__row--action"
                  tabindex="0"
                  onclick={(event) => handleRowClick(event, row)}
                  onkeydown={(event) => handleRowKeydown(event, row)}
                >
                  {@render cells(row)}
                </tr>
              {:else}
                <tr class="data-table__row">{@render cells(row)}</tr>
              {/if}
            {/each}
          </tbody>
          {#if footer}
            <tfoot>
              <tr>
                {#each columns as column (column.id)}
                  <td
                    class={[
                      'data-table__cell',
                      columnAlign(column) === 'end' && 'data-table__cell--end',
                      column.mono && 'data-table__cell--mono',
                    ]}>{@render footer(column)}</td
                  >
                {/each}
              </tr>
            </tfoot>
          {/if}
        </table>
      </div>
    {/if}
    {#if view.limited}
      <Button
        variant="tertiary"
        class="data-table__more"
        aria-expanded={expanded}
        aria-controls={tableId}
        onClick={() => (expanded = !expanded)}
      >
        {expanded
          ? t('common.dataTable.showFewer')
          : t('common.dataTable.showAll', {
              count: formatCount(view.matched, locale),
            })}
        <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"
          ><path d={expanded ? 'm6 15 6-6 6 6' : 'm6 9 6 6 6-6'} /></svg
        >
      </Button>
    {/if}
  {/if}
</div>

{#snippet cells(row)}
  {#each columns as column (column.id)}
    <td
      class={[
        'data-table__cell',
        columnAlign(column) === 'end' && 'data-table__cell--end',
        column.mono && 'data-table__cell--mono',
      ]}
    >
      {#if column.cell}
        {@render column.cell(row, column)}
      {:else}
        {cellText(row, column, locale)}
      {/if}
    </td>
  {/each}
{/snippet}
