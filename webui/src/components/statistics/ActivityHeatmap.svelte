<script>
  // The activity calendar: one column per week and one cell per day of the
  // last year, shaded by the chosen metric in five levels. The cells form
  // one Tab stop: arrow keys move by day (up/down) and week (left/right),
  // Home and End jump to the first and last day; each cell says its day
  // through its accessible name and a details tooltip. A view too narrow
  // for the whole year shows its latest weeks; levels stay those of the year.
  import { tick } from 'svelte';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { activityCalendar, dateFromKey } from '$lib/statisticsView.js';

  let {
    days = [],
    metric = 'runs',
    today,
    weekStart = 1,
    locale = 'en',
    ariaLabel = '',
    cellLabel = () => '',
    cellTooltip = () => '',
  } = $props();

  // The width one week column needs at the smallest cell, and the room the
  // weekday labels take.
  const WEEK_WIDTH = 14;
  const LABEL_WIDTH = 34;
  const MIN_WEEKS = 13;

  let width = $state(0);

  const year = $derived(activityCalendar(days, { metric, today, weekStart }));
  const shownWeeks = $derived(
    width > 0
      ? Math.max(
          MIN_WEEKS,
          Math.min(
            year.weeks.length,
            Math.floor((width - LABEL_WIDTH) / WEEK_WIDTH),
          ),
        )
      : year.weeks.length,
  );
  const calendar = $derived.by(() => {
    const first = Math.max(0, year.weeks.length - shownWeeks);
    return {
      weeks: year.weeks.slice(first),
      months: year.months
        .filter((month) => month.column >= first)
        .map((month) => ({ ...month, column: month.column - first })),
    };
  });
  // Month labels need about three columns; a month that starts too close to
  // the previous label keeps its column unlabelled.
  const monthLabels = $derived.by(() => {
    const labels = [];
    for (const month of calendar.months) {
      const previous = labels.at(-1);
      if (previous && month.column - previous.column < 3) continue;
      labels.push({ ...month, text: monthName(month.date) });
    }
    return labels;
  });
  // Weekday names of rows 1, 3 and 5, from the calendar's first full week.
  const weekdayLabels = $derived.by(() => {
    const week = calendar.weeks.find((column) => column.every(Boolean));
    return (week ?? []).map((cell, row) =>
      row % 2 === 1 ? weekdayName(cell.date) : '',
    );
  });
  const cells = $derived(calendar.weeks.flat().filter(Boolean));

  let activeDate = $state('');
  let cellElements = $state({});

  const focusDate = $derived(
    cells.some((cell) => cell.date === activeDate)
      ? activeDate
      : (cells.at(-1)?.date ?? ''),
  );

  function monthName(key) {
    return new Intl.DateTimeFormat(locale, {
      month: 'short',
      timeZone: 'UTC',
    }).format(dateFromKey(key));
  }

  function weekdayName(key) {
    return new Intl.DateTimeFormat(locale, {
      weekday: 'short',
      timeZone: 'UTC',
    }).format(dateFromKey(key));
  }

  async function moveFocus(event, column, row) {
    const weeks = calendar.weeks;
    const moves = {
      ArrowUp: () => weeks[column]?.[row - 1],
      ArrowDown: () => weeks[column]?.[row + 1],
      ArrowLeft: () => weeks[column - 1]?.[row],
      ArrowRight: () => weeks[column + 1]?.[row],
      Home: () => cells[0],
      End: () => cells.at(-1),
    };
    if (!Object.hasOwn(moves, event.key)) return;
    const target = moves[event.key]();
    event.preventDefault();
    if (!target) return;
    activeDate = target.date;
    await tick();
    cellElements[target.date]?.focus();
  }
</script>

<div
  class="stats-heatmap"
  style:--stats-heat-weeks={calendar.weeks.length}
  bind:clientWidth={width}
>
  <div class="stats-heatmap__frame">
    <div class="stats-heatmap__months" aria-hidden="true">
      {#each monthLabels as month (month.date)}
        <span style:grid-column={`${month.column + 1} / span 3`}
          >{month.text}</span
        >
      {/each}
    </div>
    <div class="stats-heatmap__weekdays" aria-hidden="true">
      {#each weekdayLabels as label, row (row)}
        <span>{label}</span>
      {/each}
    </div>
    <div class="stats-heatmap__grid" role="group" aria-label={ariaLabel}>
      {#each calendar.weeks as week, column (column)}
        {#each week as cell, row (row)}
          {#if cell}
            <button
              type="button"
              class={[
                'stats-heatmap__cell',
                `stats-heatmap__cell--${cell.level}`,
              ]}
              style:grid-column={column + 1}
              style:grid-row={row + 1}
              tabindex={cell.date === focusDate ? 0 : -1}
              aria-label={cellLabel(cell)}
              bind:this={cellElements[cell.date]}
              onfocus={() => (activeDate = cell.date)}
              onkeydown={(event) => moveFocus(event, column, row)}
              use:tooltip={() => cellTooltip(cell)}
            ></button>
          {/if}
        {/each}
      {/each}
    </div>
  </div>
  <div class="stats-heatmap__legend" aria-hidden="true">
    <span>{t('statistics.activity.less')}</span>
    {#each [0, 1, 2, 3, 4] as level (level)}
      <span class={['stats-heatmap__cell', `stats-heatmap__cell--${level}`]}
      ></span>
    {/each}
    <span>{t('statistics.activity.more')}</span>
  </div>
</div>
