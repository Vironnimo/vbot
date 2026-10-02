<script>
  // A stacked column chart for the Statistics tabs. Each column is
  // `{ key, total, segments: [{ id, value }] }`; a segment's color comes from
  // its id (`stats-fill--<id>` in report.css). The columns form one Tab stop:
  // arrow keys, Home and End move between them, and each column says its
  // values through its accessible name and a tooltip. Axis labels sit at a
  // regular stride, as many as the chart's width leaves room for.
  import { tooltip } from '$lib/tooltip.js';
  import { axisLabelIndices } from '$lib/statisticsView.js';

  let {
    columns = [],
    scaleMax = 0,
    ariaLabel = '',
    formatTick = (value) => String(value),
    axisLabel = (column) => column.label ?? column.key,
    columnLabel = (column) => String(column.total),
    columnTooltip = () => '',
    maxAxisLabels = 8,
    compact = false,
  } = $props();

  // The width one axis label needs, including the gap to its neighbour.
  const AXIS_LABEL_WIDTH = 56;

  let activeIndex = $state(-1);
  let columnElements = $state([]);
  let axisWidth = $state(0);

  const labelCount = $derived(
    axisWidth > 0
      ? Math.min(
          maxAxisLabels,
          Math.max(2, Math.floor(axisWidth / AXIS_LABEL_WIDTH)),
        )
      : maxAxisLabels,
  );
  const labelled = $derived(
    new Set(axisLabelIndices(columns.length, labelCount)),
  );
  // The column that takes the Tab stop: the one last moved to, else the
  // latest.
  const focusIndex = $derived(
    activeIndex >= 0 && activeIndex < columns.length
      ? activeIndex
      : columns.length - 1,
  );

  function height(value, total) {
    return total > 0 ? `${(Math.max(0, value) / total) * 100}%` : '0%';
  }

  function moveFocus(event, index) {
    let next = null;
    if (event.key === 'ArrowRight')
      next = Math.min(columns.length - 1, index + 1);
    else if (event.key === 'ArrowLeft') next = Math.max(0, index - 1);
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = columns.length - 1;
    if (next === null) return;
    event.preventDefault();
    activeIndex = next;
    columnElements[next]?.focus();
  }
</script>

<div
  class="stats-chart"
  class:stats-chart--compact={compact}
  role="group"
  aria-label={ariaLabel}
>
  <div class="stats-chart__y" aria-hidden="true">
    <span>{formatTick(scaleMax)}</span>
    <span>{formatTick(scaleMax / 2)}</span>
    <span>{formatTick(0)}</span>
  </div>
  <div class="stats-chart__plot">
    <div class="stats-chart__grid" aria-hidden="true">
      <span></span><span></span><span></span>
    </div>
    <div class="stats-chart__columns">
      {#each columns as column, index (column.key)}
        <button
          type="button"
          class="stats-chart__column"
          tabindex={index === focusIndex ? 0 : -1}
          aria-label={columnLabel(column)}
          bind:this={columnElements[index]}
          onfocus={() => (activeIndex = index)}
          onkeydown={(event) => moveFocus(event, index)}
          use:tooltip={() => columnTooltip(column)}
        >
          <span
            class="stats-chart__bar"
            class:stats-chart__bar--visible={column.total > 0}
            style:height={height(column.total, scaleMax)}
          >
            {#each column.segments as segment (segment.id)}
              <span
                class={['stats-chart__segment', `stats-fill--${segment.id}`]}
                style:height={height(segment.value, column.total)}
              ></span>
            {/each}
          </span>
        </button>
      {/each}
    </div>
  </div>
  <div class="stats-chart__x" aria-hidden="true" bind:clientWidth={axisWidth}>
    {#each columns as column, index (column.key)}
      <span>{labelled.has(index) ? axisLabel(column) : ''}</span>
    {/each}
  </div>
</div>
