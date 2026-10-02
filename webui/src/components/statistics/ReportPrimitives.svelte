<script module>
  // Shared markup of the Statistics tabs: KPI tiles, Agent, Session and
  // Model names, dates, token and cost values with their exact amounts on
  // hover or focus, and compact bar lists.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { formatMoment } from '$lib/timeText.js';
  import Badge from '../ui/Badge.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    agentDisplay,
    agentTooltip,
    formatCost,
    formatInteger,
    formatShortDateTime,
    formatTokens,
    originLabel,
    sessionTooltip,
    tokenTooltip,
  } from '$lib/statisticsView.js';
  export {
    kpiTile,
    agentName,
    sessionName,
    sessionWithAgent,
    originName,
    idCell,
    dateCell,
    tokenValue,
    costValue,
    barList,
  };
</script>

<!-- A KPI tile: `{ label, value, valueTooltip?, hint?, change?, detail?,
     detailTooltip?, warning? }`. `change` is a periodChange() result with
     an optional `tooltip`; `warning` marks the value as uncertain with a
     marker beside the label that says why on hover or focus. -->
{#snippet kpiTile(tile)}
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
{/snippet}

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

<!-- One tooltip for the whole Agent cell: the Project and full address of a
     Project Agent, what an Extension's Sessions are, or a truncated name.
     The name shortens; its badge never does. -->
{#snippet agentName(agentId)}
  {@const display = agentDisplay(agentId)}
  <span class="stats-agent" use:tooltip={agentTooltip(agentId)}>
    <span class="stats-agent__name">{display.name}</span>
    {#if display.projectId}
      <Badge class="stats-agent__badge" variant="info"
        >{display.projectId}</Badge
      >
    {:else if display.extension}
      <Badge class="stats-agent__badge" variant="neutral"
        >{t('statistics.agent.extensionBadge')}</Badge
      >
    {/if}
  </span>
{/snippet}

<!-- A Session named by its title (id as a tooltip row), or by its id. -->
{#snippet sessionName(row)}
  <span
    class="stats-session"
    class:stats-mono={!row.session_title}
    use:tooltip={sessionTooltip(row)}
    >{row.session_title || row.session_id}</span
  >
{/snippet}

<!-- A Session with its Agent below it, for tables of Runs and Sessions. -->
{#snippet sessionWithAgent(row)}
  <span class="stats-stack">
    {@render sessionName(row)}
    <span class="stats-stack__secondary">{@render agentName(row.agent_id)}</span
    >
  </span>
{/snippet}

{#snippet originName(origin)}
  <span class="stats-origin-name">
    <span class={['stats-swatch', `stats-swatch--${origin}`]} aria-hidden="true"
    ></span>
    {originLabel(origin)}
  </span>
{/snippet}

<!-- Table cells (DataTable `cell`) for the row's value under the column id:
     a code-like id (Model, Tool) on one line, shortened with the full id on
     hover when cut; a short date and time on one line, the full moment on
     hover. -->
{#snippet idCell(row, column)}
  {@const id = row?.[column.id]}
  {#if id}
    <span
      class="stats-id"
      use:tooltip={{ text: id, mono: true, whenTruncated: true }}>{id}</span
    >
  {:else}
    —
  {/if}
{/snippet}

{#snippet dateCell(row, column)}
  {@const isoString = row?.[column.id]}
  <span class="stats-date" use:tooltip={() => formatMoment(isoString)}
    >{formatShortDateTime(isoString, activeLocaleTag())}</span
  >
{/snippet}

<!-- Compact tokens with the exact count (and estimated part) on hover or
     focus. -->
{#snippet tokenValue(value, estimated = 0)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the exact count here.) -->
  <span
    class="stats-number"
    class:stats-number--estimated={estimated > 0}
    tabindex="0"
    use:tooltip={() => tokenTooltip(value, estimated, activeLocaleTag())}
    >{formatTokens(value, activeLocaleTag())}</span
  >
{/snippet}

<!-- A cost with its basis (or exact amount) on hover or focus. -->
{#snippet costValue(amount, basis = '')}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the cost basis here.) -->
  <span
    class="stats-number"
    tabindex={basis ? 0 : undefined}
    use:tooltip={basis}>{formatCost(amount, activeLocaleTag())}</span
  >
{/snippet}

<!-- Horizontal bars, each relative to the largest: `entries` from
     barEntries(); `label(entry)` renders the name, `valueText(entry)` the
     value (counts by default). -->
{#snippet barList(entries, label, valueText = null, ariaLabel = '')}
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
{/snippet}
