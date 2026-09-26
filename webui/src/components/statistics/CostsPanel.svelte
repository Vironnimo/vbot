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
  import { statCard, agentName } from './ReportPrimitives.svelte';
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
      return t('statistics.cost.providerSource', 'Provider');
    if (call.cost.source === 'catalog')
      return call.retrospective
        ? t('statistics.cost.currentCatalog', 'Current catalog estimate')
        : t('statistics.cost.savedCatalog', 'Saved catalog estimate');
    return t('statistics.cost.unknown', 'Unpriced');
  }
  function reasonLabel(reason) {
    return (
      {
        missing_usage: t(
          'statistics.cost.reason.usage',
          'Token usage unavailable',
        ),
        missing_price: t(
          'statistics.cost.reason.price',
          'No matching catalog price',
        ),
        unsupported_tier: t(
          'statistics.cost.reason.tier',
          'Unsupported price schedule',
        ),
        invalid_cache: t(
          'statistics.cost.reason.cache',
          'Inconsistent cache counters',
        ),
        invalid_reasoning: t(
          'statistics.cost.reason.reasoning',
          'Inconsistent Reasoning counters',
        ),
        missing_reasoning_usage: t(
          'statistics.cost.reason.reasoningUsage',
          'Reasoning usage unavailable',
        ),
        missing_bucket_price: t(
          'statistics.cost.reason.bucket',
          'A used token category has no price',
        ),
      }[reason] ?? t('statistics.cost.unknown', 'Unpriced')
    );
  }
</script>

<div class="stats-block">
  <h3 class="stats-block__title">
    {t('statistics.cost.title', 'Cost & API-equivalent usage · USD')}
  </h3>
  <div class="stats-grid stats-grid--three">
    {@render statCard(
      t('statistics.cost.reported', 'Provider-reported cost'),
      formatCost(costs.totals.reported_usd, locale),
      null,
      t('statistics.cost.callCount', '{count} calls', {
        count: formatInteger(costs.totals.reported_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.estimated', 'Estimated API value'),
      formatCost(costs.totals.estimated_usd, locale),
      null,
      t('statistics.cost.callCount', '{count} calls', {
        count: formatInteger(costs.totals.estimated_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.unpriced', 'Calls without a price'),
      formatInteger(costs.totals.unpriced_calls, locale),
      null,
      t('statistics.cost.ofCalls', 'of {count} recorded calls', {
        count: formatInteger(costs.totals.calls, locale),
      }),
    )}
  </div>
  <p class="stats-note stats-spaced">
    {t(
      'statistics.cost.explanation',
      'Provider amounts and catalog estimates cover different calls and are shown separately. Estimates account for cache pricing and context tiers. Subscription usage is valued at API prices; this is not your subscription bill.',
    )}
  </p>
  <p class="stats-note stats-spaced">
    {t(
      'statistics.cost.historical',
      '{count} older calls use current catalog prices. New calls keep their original price snapshot.',
      { count: formatInteger(costs.totals.retrospective_calls, locale) },
    )}
  </p>
  <p class="stats-note stats-spaced">
    {t(
      'statistics.cost.scope',
      'Includes Chat, Compaction, Task Models and background Model requests, including retries. Recorded usage remains after a Session is archived or deleted. Older requests count where usage was retained; missing tokens and prices stay unknown. Session diagnostics cover retained, unarchived Sessions.',
    )}
  </p>
</div>
<div class="stats-block">
  <h3 class="stats-block__title">
    {t('statistics.cost.models', 'Cost by Model')}
  </h3>
  {#if costs.models.length === 0}
    <EmptyState
      density="compact"
      description={t('statistics.empty', 'No activity recorded yet.')}
    />
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
    <div
      class="stats-table-scroll"
      role="region"
      tabindex="0"
      aria-label={t('statistics.cost.models', 'Cost by Model')}
    >
      <table class="stats-table">
        <thead
          ><tr>
            <th>{t('statistics.col.model', 'Model')}</th><th
              >{t('statistics.cost.callsShort', 'Calls')}</th
            >
            <th>{t('statistics.cost.reported', 'Provider-reported cost')}</th
            ><th>{t('statistics.cost.estimated', 'Estimated API value')}</th>
            <th>{t('statistics.cost.unknown', 'Unpriced')}</th>
          </tr></thead
        ><tbody
          >{#each costs.models as row (row.model)}
            <tr
              ><td class="stats-mono stats-wrap">{row.model}</td><td
                >{formatInteger(row.totals.calls, locale)}</td
              >
              <td>{formatCost(row.totals.reported_usd, locale)}</td><td
                >{formatCost(row.totals.estimated_usd, locale)}</td
              >
              <td>{formatInteger(row.totals.unpriced_calls, locale)}</td></tr
            >
          {/each}</tbody
        >
      </table>
    </div>
  {/if}
  <details class="stats-details">
    <summary>{t('statistics.cost.daily', 'Daily cost · UTC')}</summary>
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
    <div
      class="stats-table-scroll"
      role="region"
      tabindex="0"
      aria-label={t('statistics.cost.daily', 'Daily cost · UTC')}
    >
      <table class="stats-table">
        <thead
          ><tr
            ><th>{t('statistics.col.date', 'Date')}</th>
            <th>{t('statistics.cost.reported', 'Provider-reported cost')}</th
            ><th>{t('statistics.cost.estimated', 'Estimated API value')}</th><th
              >{t('statistics.cost.unknown', 'Unpriced')}</th
            >
          </tr></thead
        ><tbody
          >{#each costs.daily as row (row.date)}<tr>
              <td>{formatDate(row.date, locale)}</td><td
                >{formatCost(row.totals.reported_usd, locale)}</td
              >
              <td>{formatCost(row.totals.estimated_usd, locale)}</td><td
                >{formatInteger(row.totals.unpriced_calls, locale)}</td
              >
            </tr>{/each}</tbody
        >
      </table>
    </div>
  </details>
  <details class="stats-details">
    <summary
      >{t(
        'statistics.cost.sessions',
        'Sessions with the highest recorded cost · top 20',
      )}</summary
    >
    <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
    <div
      class="stats-table-scroll"
      role="region"
      tabindex="0"
      aria-label={t(
        'statistics.cost.sessions',
        'Sessions with the highest recorded cost · top 20',
      )}
    >
      <table class="stats-table">
        <thead
          ><tr
            ><th>{t('statistics.col.session', 'Session')}</th><th
              >{t('statistics.col.agent', 'Agent')}</th
            >
            <th>{t('statistics.cost.reported', 'Provider-reported cost')}</th
            ><th>{t('statistics.cost.estimated', 'Estimated API value')}</th><th
              >{t('statistics.cost.unknown', 'Unpriced')}</th
            >
          </tr></thead
        ><tbody
          >{#each costs.top_sessions as row (`${row.agent_id}:${row.session_id}`)}<tr
            >
              <td class="stats-wrap">{row.session_title || row.session_id}</td
              ><td>{@render agentName(row.agent_id)}</td>
              <td>{formatCost(row.totals.reported_usd, locale)}</td><td
                >{formatCost(row.totals.estimated_usd, locale)}</td
              >
              <td>{formatInteger(row.totals.unpriced_calls, locale)}</td>
            </tr>{/each}</tbody
        >
      </table>
    </div>
  </details>
</div>
<div class="stats-block">
  <h3 class="stats-block__title">
    {t('statistics.cost.recent', 'Recent Model calls')}
  </h3>
  <p class="stats-note">
    {t(
      'statistics.cost.recentHint',
      'Up to 50 calls in this time range, newest first. Expand a call for its price source and Session.',
    )}
  </p>
  {#if costs.recent_calls.length === 0}
    <EmptyState
      density="compact"
      description={t('statistics.empty', 'No activity recorded yet.')}
    />
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
              {modelCallKindLabel(call.kind, t)}
              {#if call.status}
                · {modelCallStatusLabel(call.status, t)}{/if}
              {#if call.session_id}
                · {call.session_title || call.session_id}
              {:else}
                · {t('statistics.cost.withoutSession', 'Outside a Session')}
              {/if}
              {#if call.agent_id}
                · {@render agentName(call.agent_id)}{/if}
            </p>
            <div class="stats-grid stats-spaced">
              {@render statCard(
                t('statistics.col.input', 'Input'),
                formatOptionalTokens(call.input_tokens, locale),
              )}
              {@render statCard(
                t('statistics.col.output', 'Output'),
                formatOptionalTokens(call.output_tokens, locale),
              )}
              {@render statCard(
                t('statistics.usage.cacheRead', 'Cache read'),
                formatOptionalTokens(call.cache_read_tokens, locale),
              )}
              {@render statCard(
                t('statistics.cost.source', 'Price source'),
                call.cost.pricing?.source ?? sourceLabel(call),
              )}
            </div>
            {#if call.estimated_tokens}<p class="stats-note stats-spaced">
                {t(
                  'statistics.estimatedHint',
                  'Token usage includes estimates.',
                )}
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
