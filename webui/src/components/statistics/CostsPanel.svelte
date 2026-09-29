<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatCost,
    formatDate,
    formatDateTime,
    formatInteger,
    formatOptionalTokens,
    modelCallKindLabel,
    modelCallStatusLabel,
  } from '$lib/statisticsView.js';
  import {
    statCard,
    agentName,
    sessionName,
    costCell,
  } from './ReportPrimitives.svelte';
  let { report } = $props();
  const locale = $derived(activeLocaleTag());
  const costs = $derived(
    report.costs ?? {
      totals: {},
      models: [],
      daily: [],
      top_sessions: [],
      recent_calls: [],
    },
  );
  function sourceLabel(call) {
    if (call.cost.source === 'provider')
      return t('statistics.cost.providerSource');
    if (call.cost.source === 'catalog')
      return call.retrospective
        ? t('statistics.cost.currentCatalog')
        : t('statistics.cost.savedCatalog');
    return t('statistics.cost.unknown');
  }
  function reasonLabel(reason) {
    return (
      {
        missing_usage: t('statistics.cost.reason.usage'),
        missing_price: t('statistics.cost.reason.price'),
        unsupported_tier: t('statistics.cost.reason.tier'),
        invalid_cache: t('statistics.cost.reason.cache'),
        invalid_reasoning: t('statistics.cost.reason.reasoning'),
        missing_reasoning_usage: t('statistics.cost.reason.reasoningUsage'),
        missing_bucket_price: t('statistics.cost.reason.bucket'),
      }[reason] ?? t('statistics.cost.unknown')
    );
  }
</script>

<div class="stats-block">
  <h3 class="stats-block__title">
    {t('statistics.cost.title')}
  </h3>
  <div class="stats-grid stats-grid--three">
    {@render statCard(
      t('statistics.cost.reported'),
      formatCost(costs.totals.reported_usd, locale),
      null,
      t('statistics.cost.callCount', {
        count: formatInteger(costs.totals.reported_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.estimated'),
      formatCost(costs.totals.estimated_usd, locale),
      t('statistics.cost.subscriptionHint'),
      t('statistics.cost.callCount', {
        count: formatInteger(costs.totals.estimated_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.unpriced'),
      formatInteger(costs.totals.unpriced_calls, locale),
      t('statistics.cost.unpricedHint'),
      t('statistics.cost.ofCalls', {
        count: formatInteger(costs.totals.calls, locale),
      }),
    )}
  </div>
  <p class="stats-note stats-spaced">
    {t('statistics.cost.explanation')}
  </p>
  <p class="stats-note stats-spaced">
    {t('statistics.cost.historical', {
      count: formatInteger(costs.totals.retrospective_calls, locale),
    })}
  </p>
  <p class="stats-note stats-spaced">
    {t('statistics.cost.scope')}
  </p>
</div>
<div class="stats-block">
  <h3 class="stats-block__title">
    {t('statistics.cost.models')}
  </h3>
  {#if costs.models.length === 0}
    <EmptyState density="compact" description={t('statistics.empty')} />
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
    <div
      class="stats-table-scroll"
      role="region"
      tabindex="0"
      aria-label={t('statistics.cost.models')}
    >
      <table class="stats-table">
        <thead
          ><tr>
            <th>{t('statistics.col.model')}</th><th
              >{t('statistics.cost.callsShort')}</th
            >
            <th>{t('statistics.cost.reported')}</th><th
              >{t('statistics.cost.estimated')}</th
            >
            <th>{t('statistics.cost.unknown')}</th>
          </tr></thead
        ><tbody
          >{#each costs.models as row (row.model)}
            <tr
              ><td class="stats-mono stats-wrap">{row.model}</td><td
                >{formatInteger(row.totals.calls, locale)}</td
              >
              <td>{@render costCell(row.totals, 'reported')}</td><td
                >{@render costCell(row.totals, 'estimated')}</td
              >
              <td>{@render costCell(row.totals, 'unpriced')}</td></tr
            >
          {/each}</tbody
        >
      </table>
    </div>
  {/if}
  <details class="stats-details">
    <summary>{t('statistics.cost.daily')}</summary>
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
    <div
      class="stats-table-scroll"
      role="region"
      tabindex="0"
      aria-label={t('statistics.cost.daily')}
    >
      <table class="stats-table">
        <thead
          ><tr
            ><th>{t('statistics.col.date')}</th>
            <th>{t('statistics.cost.reported')}</th><th
              >{t('statistics.cost.estimated')}</th
            ><th>{t('statistics.cost.unknown')}</th>
          </tr></thead
        ><tbody
          >{#each costs.daily as row (row.date)}<tr>
              <td>{formatDate(row.date, locale)}</td><td
                >{@render costCell(row.totals, 'reported')}</td
              >
              <td>{@render costCell(row.totals, 'estimated')}</td><td
                >{@render costCell(row.totals, 'unpriced')}</td
              >
            </tr>{/each}</tbody
        >
      </table>
    </div>
  </details>
  <details class="stats-details">
    <summary>{t('statistics.cost.sessions')}</summary>
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
    <div
      class="stats-table-scroll"
      role="region"
      tabindex="0"
      aria-label={t('statistics.cost.sessions')}
    >
      <table class="stats-table">
        <thead
          ><tr
            ><th>{t('statistics.col.session')}</th><th
              >{t('statistics.col.agent')}</th
            >
            <th>{t('statistics.cost.reported')}</th><th
              >{t('statistics.cost.estimated')}</th
            ><th>{t('statistics.cost.unknown')}</th>
          </tr></thead
        ><tbody
          >{#each costs.top_sessions as row (`${row.agent_id}:${row.session_id}`)}<tr
            >
              <td>{@render sessionName(row)}</td><td
                >{@render agentName(row.agent_id)}</td
              >
              <td>{@render costCell(row.totals, 'reported')}</td><td
                >{@render costCell(row.totals, 'estimated')}</td
              >
              <td>{@render costCell(row.totals, 'unpriced')}</td>
            </tr>{/each}</tbody
        >
      </table>
    </div>
  </details>
</div>
<div class="stats-block">
  <h3 class="stats-block__title">
    {t('statistics.cost.recent')}
  </h3>
  <p class="stats-note">
    {t('statistics.cost.recentHint')}
  </p>
  {#if costs.recent_calls.length === 0}
    <EmptyState density="compact" description={t('statistics.empty')} />
  {:else}<div class="stats-call-list">
      {#each costs.recent_calls as call (call)}<details class="stats-call">
          <summary
            ><span class="stats-call__time"
              >{formatDateTime(call.timestamp, locale)}</span
            >
            <span class="stats-mono stats-wrap">{call.model}</span><span
              class="stats-call__source">{sourceLabel(call)}</span
            >
            <strong
              >{formatCost(call.cost.amount_usd, locale, {
                exact: true,
              })}</strong
            ></summary
          >
          <div class="stats-call__body">
            <p class="stats-note">
              {modelCallKindLabel(call.kind)}
              {#if call.status}
                · {modelCallStatusLabel(call.status)}{/if}
              {#if call.session_id}
                · {@render sessionName(call)}
              {:else}
                · {t('statistics.cost.withoutSession')}
              {/if}
              {#if call.agent_id}
                · {@render agentName(call.agent_id)}{/if}
            </p>
            <div class="stats-grid stats-spaced">
              {@render statCard(
                t('statistics.col.input'),
                formatOptionalTokens(call.input_tokens, locale),
              )}
              {@render statCard(
                t('statistics.col.output'),
                formatOptionalTokens(call.output_tokens, locale),
              )}
              {@render statCard(
                t('statistics.usage.cacheRead'),
                formatOptionalTokens(call.cache_read_tokens, locale),
              )}
              {@render statCard(
                t('statistics.cost.source'),
                call.cost.pricing?.source ?? sourceLabel(call),
              )}
            </div>
            {#if call.estimated_tokens}<p class="stats-note stats-spaced">
                {t('statistics.estimatedHint')}
              </p>{/if}
            {#if call.cost.source === 'unknown'}<p
                class="stats-note stats-spaced"
              >
                {reasonLabel(call.cost.reason)}
              </p>{/if}
          </div>
        </details>{/each}
    </div>{/if}
</div>
