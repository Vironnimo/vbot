<script module>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    agentDisplay,
    agentTooltip,
    costCellTooltip,
    formatCost,
    formatInteger,
    formatShare,
    formatTokens,
    sessionTooltip,
    tokenBreakdownTooltip,
    tokenSplit,
    topN,
  } from '$lib/statisticsView.js';
  export {
    statCard,
    agentName,
    sessionName,
    barRows,
    tokenCell,
    tokensHeader,
    costCell,
    countTable,
    agentCountTable,
  };
</script>

<!-- `detailTooltip` breaks the detail line down further (for example a
     "not completed" count into its statuses). -->
{#snippet statCard(label, value, hint, detail, detailTooltip = '')}
  <div class="stats-card">
    <span class="stats-card__label">
      {label}
      {#if hint}
        <InfoHint text={hint} />
      {/if}
    </span>
    <span class="stats-card__value">{value}</span>
    {#if detail}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the breakdown here.) -->
      <span
        class="stats-card__detail"
        class:stats-card__detail--more={Boolean(detailTooltip)}
        tabindex={detailTooltip ? 0 : undefined}
        use:tooltip={detailTooltip}>{detail}</span
      >
    {/if}
  </div>
{/snippet}

<!-- One tooltip for the whole Agent cell: the Project and full address of a
     Project Agent, what an Extension's Sessions are, or a truncated name. -->
{#snippet agentName(agentId)}
  {@const display = agentDisplay(agentId)}
  <span class="stats-agent" use:tooltip={agentTooltip(agentId)}>
    <span class="stats-agent__name">{display.name}</span>
    {#if display.projectId}
      <span class="stats-agent__project">
        <Badge variant="info">{display.projectId}</Badge>
      </span>
    {:else if display.extension}
      <span class="stats-agent__project">
        <Badge variant="neutral">{t('statistics.agent.extensionBadge')}</Badge>
      </span>
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

{#snippet barRows(entries, total)}
  <ul class="stats-bars">
    {#each entries as entry (entry.label)}
      <li class="stats-bars__row">
        <span
          class="stats-bars__label"
          use:tooltip={{ text: entry.label, whenTruncated: true }}
          >{entry.label}</span
        >
        <span class="stats-bars__track">
          <span
            class="stats-bars__fill"
            style={`width: ${Math.round(entry.fraction * 100)}%`}
          ></span>
        </span>
        <span class="stats-bars__value"
          >{formatInteger(entry.value, activeLocaleTag())}</span
        >
        {#if total}
          <span class="stats-bars__share"
            >{formatShare(entry.value, total)}</span
          >
        {/if}
      </li>
    {/each}
  </ul>
{/snippet}

<!-- Measured tokens, the separately kept estimate in amber, and the full
     breakdown on hover or focus. Pass `focusable = false` inside an already
     interactive element (a <summary>). -->
{#snippet tokenCell(record, focusable = true)}
  {@const split = tokenSplit(record)}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the token breakdown here.) -->
  <span
    class="stats-tokens"
    tabindex={focusable ? 0 : undefined}
    use:tooltip={() => tokenBreakdownTooltip(record, activeLocaleTag())}
  >
    <span>{formatTokens(split.measured, activeLocaleTag())}</span>
    {#if split.hasEstimated}
      <span class="stats-tokens__est"
        >+~{formatTokens(split.estimated, activeLocaleTag())}</span
      >
    {/if}
  </span>
{/snippet}

<!-- A cost table cell ('reported', 'estimated' or 'unpriced') with the calls
     it covers and its exact amount. -->
{#snippet costCell(totals, kind)}
  {@const content = costCellTooltip(totals, kind, activeLocaleTag())}
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the cost basis here.) -->
  <span
    class="stats-value"
    tabindex={content ? 0 : undefined}
    use:tooltip={content}
    >{kind === 'unpriced'
      ? formatInteger(totals?.unpriced_calls, activeLocaleTag())
      : formatCost(
          kind === 'reported' ? totals?.reported_usd : totals?.estimated_usd,
          activeLocaleTag(),
        )}</span
  >
{/snippet}

<!-- The Tokens column header explains measured and estimated values once. -->
{#snippet tokensHeader(label)}
  <span class="stats-th-hint"
    >{label}<InfoHint text={t('statistics.tokens.columnHint')} /></span
  >
{/snippet}

{#snippet countTable(title, entries)}
  <div class="stats-block stats-block--narrow">
    <h3 class="stats-block__title">{title}</h3>
    {#if entries.length === 0}
      <EmptyState density="compact" description={t('statistics.none')} />
    {:else}
      {@render barRows(
        topN(entries, 8).map((entry) => ({
          label: entry.key,
          value: entry.count,
          fraction: entries[0].count ? entry.count / entries[0].count : 0,
        })),
        null,
      )}
      {#if entries.length > 8}<details class="stats-details">
          <summary
            >{t('statistics.ranking.more', {
              count: formatInteger(entries.length - 8, activeLocaleTag()),
            })}</summary
          >{@render barRows(
            entries.slice(8).map((entry) => ({
              label: entry.key,
              value: entry.count,
              fraction: entries[0].count ? entry.count / entries[0].count : 0,
            })),
            null,
          )}
        </details>{/if}
    {/if}
  </div>
{/snippet}

{#snippet agentCountTable(title, entries)}
  <div class="stats-block stats-block--narrow">
    <h3 class="stats-block__title">{title}</h3>
    {#if entries.length === 0}
      <EmptyState density="compact" description={t('statistics.none')} />
    {:else}
      <ul class="stats-bars">
        {#each topN(entries, 8) as entry (entry.key)}
          <li class="stats-bars__row">
            <span class="stats-bars__label">{@render agentName(entry.key)}</span
            >
            <span class="stats-bars__track">
              <span
                class="stats-bars__fill"
                style={`width: ${Math.round((entries[0].count ? entry.count / entries[0].count : 0) * 100)}%`}
              ></span>
            </span>
            <span class="stats-bars__value"
              >{formatInteger(entry.count, activeLocaleTag())}</span
            >
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/snippet}
