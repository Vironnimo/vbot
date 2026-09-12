<script module>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import Badge from '../ui/Badge.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    agentDisplay,
    formatInteger,
    formatShare,
    formatTokens,
    tokenSplit,
    topN,
  } from '$lib/statisticsView.js';
  export {
    statCard,
    estimatedBadge,
    agentName,
    barRows,
    tokenCell,
    countTable,
    agentCountTable,
  };
</script>

{#snippet statCard(label, value, hint, detail)}
  <div class="stats-card">
    <span class="stats-card__label">
      {label}
      {#if hint}
        <InfoHint text={hint} />
      {/if}
    </span>
    <span class="stats-card__value">{value}</span>
    {#if detail}<span class="stats-card__detail">{detail}</span>{/if}
  </div>
{/snippet}

{#snippet estimatedBadge()}
  <span
    class="tooltip-anchor"
    use:tooltip={t(
      'statistics.estimatedHint',
      'Estimated tokens are approximated, not provider-reported.',
    )}
  >
    <Badge variant="warn">
      {t('statistics.estimatedBadge', '~ estimated')}
    </Badge>
  </span>
{/snippet}

{#snippet agentName(agentId)}
  {@const display = agentDisplay(agentId)}
  <span class="stats-agent">
    <span class="stats-agent__name">{display.name}</span>
    {#if display.projectId}
      <span
        class="stats-agent__project tooltip-anchor"
        use:tooltip={t(
          'statistics.agent.projectBadgeTitle',
          'Project: {project}',
          {
            project: display.projectId,
          },
        )}
      >
        <Badge variant="info">{display.projectId}</Badge>
      </span>
    {/if}
  </span>
{/snippet}

{#snippet barRows(entries, total)}
  <ul class="stats-bars">
    {#each entries as entry (entry.label)}
      <li class="stats-bars__row">
        <span class="stats-bars__label" use:tooltip={entry.label}
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

{#snippet tokenCell(record)}
  {@const split = tokenSplit(record)}
  <span class="stats-tokens">
    <span>{formatTokens(split.measured, activeLocaleTag())}</span>
    {#if split.hasEstimated}
      <span class="stats-tokens__est"
        >+{formatTokens(split.estimated, activeLocaleTag())}</span
      >
      {@render estimatedBadge()}
    {/if}
  </span>
{/snippet}

{#snippet countTable(title, entries)}
  <div class="stats-block stats-block--narrow">
    <h3 class="stats-block__title">{title}</h3>
    {#if entries.length === 0}
      <EmptyState
        density="compact"
        description={t('statistics.none', 'None')}
      />
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
            >{t('statistics.ranking.more', '{count} more', {
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
      <EmptyState
        density="compact"
        description={t('statistics.none', 'None')}
      />
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
