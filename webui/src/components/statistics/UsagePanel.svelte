<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import {
    cacheHitRate,
    formatDateTime,
    formatDurationMs,
    formatInteger,
    formatPercent,
    formatShare,
    formatTokens,
    groupModelsByProvider,
    modelCallKindLabel,
  } from '$lib/statisticsView.js';
  import { statCard, agentName, tokenCell } from './ReportPrimitives.svelte';
  import TokenTrend from './TokenTrend.svelte';
  import CostsPanel from './CostsPanel.svelte';

  let { report, granularity = $bindable(), reportRange } = $props();

  const locale = $derived(activeLocaleTag());

  const usage = $derived(report?.usage ?? null);

  const providerGroups = $derived(
    usage ? groupModelsByProvider(usage.models) : [],
  );

  const cacheSessions = $derived(usage?.cache?.lowest_hit_rate_sessions ?? []);

  const cacheBreaks = $derived(usage?.cache?.suspected_breaks ?? null);

  const usageTotalTokens = $derived(
    usage
      ? usage.totals.measured_input_tokens +
          usage.totals.measured_output_tokens +
          usage.totals.estimated_input_tokens +
          usage.totals.estimated_output_tokens
      : 0,
  );

  function formatReasoningTokens(record) {
    return record?.reasoning_turns > 0
      ? formatTokens(record.reasoning_tokens, locale)
      : '—';
  }
</script>

<div class="stats-panel">
  <div class="stats-columns">
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.measuredTokens')}
      </h3>
      <div class="stats-grid stats-grid--three">
        {@render statCard(
          t('statistics.col.input'),
          formatTokens(usage.totals.measured_input_tokens, locale),
        )}{@render statCard(
          t('statistics.col.output'),
          formatTokens(usage.totals.measured_output_tokens, locale),
        )}{@render statCard(
          t('statistics.usage.measuredTurns'),
          formatInteger(usage.totals.measured_turns, locale),
        )}
      </div>
    </div>
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.estimatedTokens')}<InfoHint
          text={t('statistics.estimatedHint')}
        />
      </h3>
      <div class="stats-grid stats-grid--three">
        {@render statCard(
          t('statistics.col.input'),
          formatTokens(usage.totals.estimated_input_tokens, locale),
        )}{@render statCard(
          t('statistics.col.output'),
          formatTokens(usage.totals.estimated_output_tokens, locale),
        )}{@render statCard(
          t('statistics.usage.estimatedTurns'),
          formatInteger(usage.totals.estimated_turns, locale),
        )}
      </div>
    </div>
  </div>
  {#if usage.kinds?.length}
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.byKind')}
      </h3>
      <div class="stats-table-wrap">
        <table class="stats-table" aria-label={t('statistics.usage.byKind')}>
          <thead
            ><tr>
              <th>{t('statistics.col.activity')}</th>
              <th class="num">{t('statistics.col.calls')}</th>
              <th class="num">{t('statistics.usage.incomplete')}</th>
            </tr></thead
          >
          <tbody>
            {#each usage.kinds as row (row.kind)}
              <tr>
                <td>{modelCallKindLabel(row.kind)}</td>
                <td class="num">{formatInteger(row.calls, locale)}</td>
                <td class="num"
                  >{formatInteger(row.unreported_calls, locale)}</td
                >
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}
  <TokenTrend {report} bind:granularity {reportRange} />
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.usage.cacheAndReasoning')}<InfoHint
        text={t('statistics.usage.reasoningHint')}
      />
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.usage.cacheHitRate'),
        formatPercent(cacheHitRate(usage.totals)),
        t('statistics.usage.cacheHitHint'),
      )}{@render statCard(
        t('statistics.usage.cacheRead'),
        usage.totals.cache_turns > 0
          ? formatTokens(usage.totals.cache_read_tokens, locale)
          : '—',
      )}{@render statCard(
        t('statistics.usage.cacheWrite'),
        usage.totals.cache_turns > 0
          ? formatTokens(usage.totals.cache_write_tokens, locale)
          : '—',
      )}{@render statCard(
        t('statistics.usage.reasoning'),
        formatReasoningTokens(usage.totals),
      )}
    </div>
  </div>
  <CostsPanel {report} />
  <div class="stats-block__head">
    <h3 class="stats-section-title">
      {t('statistics.usage.breakdown')}
    </h3>
    <InfoHint text={t('statistics.usage.runAttributionHint')} />
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.usage.providers')}
    </h3>
    {#if usage.providers.length === 0}
      <EmptyState density="compact" description={t('statistics.empty')} />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.table.scroll')}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.provider')}</th>
              <th>{t('statistics.col.runs')}</th>
              <th>{t('statistics.col.tokens')}</th>
              <th>{t('statistics.col.reasoning')}</th>
              <th>{t('statistics.col.cacheHit')}</th>
              <th>{t('statistics.col.share')}</th>
              <th>{t('statistics.col.errors')}</th>
            </tr>
          </thead>
          <tbody>
            {#each usage.providers as provider (provider.provider)}
              <tr>
                <td class="stats-mono">{provider.provider}</td>
                <td>{formatInteger(provider.runs, locale)}</td>
                <td>{@render tokenCell(provider)}</td>
                <td>{formatReasoningTokens(provider)}</td>
                <td>{formatPercent(cacheHitRate(provider))}</td>
                <td>{formatShare(provider.total_tokens, usageTotalTokens)}</td>
                <td>{formatInteger(provider.errors, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.usage.models')}
    </h3>
    {#each providerGroups as group (group.provider)}
      <h4 class="stats-subheading stats-mono">{group.provider}</h4>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.table.scroll')}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.model')}</th>
              <th>{t('statistics.col.runs')}</th>
              <th>{t('statistics.col.tokens')}</th>
              <th>{t('statistics.col.reasoning')}</th>
              <th>{t('statistics.col.cacheHit')}</th>
              <th>{t('statistics.col.avgDuration')}</th>
              <th>{t('statistics.col.errors')}</th>
            </tr>
          </thead>
          <tbody>
            {#each group.models as model (model.model)}
              <tr>
                <td class="stats-mono">{model.model}</td>
                <td>{formatInteger(model.runs, locale)}</td>
                <td>{@render tokenCell(model)}</td>
                <td>{formatReasoningTokens(model)}</td>
                <td>{formatPercent(cacheHitRate(model))}</td>
                <td>{formatDurationMs(model.average_run_duration_ms)}</td>
                <td>{formatInteger(model.errors, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/each}
  </div>

  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.usage.cacheSessions')}
    </h3>
    {#if cacheSessions.length === 0}
      <EmptyState
        density="compact"
        description={t('statistics.usage.cacheEmpty')}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t('statistics.table.scroll')}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.agent')}</th>
              <th>{t('statistics.col.session')}</th>
              <th>{t('statistics.col.turns')}</th>
              <th>{t('statistics.col.input')}</th>
              <th>{t('statistics.col.cacheRead')}</th>
              <th>{t('statistics.col.hitRate')}</th>
              <th>{t('statistics.col.lastActivity')}</th>
            </tr>
          </thead>
          <tbody>
            {#each cacheSessions as record (`${record.agent_id}:${record.session_id}`)}
              <tr>
                <td>{@render agentName(record.agent_id)}</td>
                <td class="stats-mono stats-truncate">{record.session_id}</td>
                <td>{formatInteger(record.cache_turns, locale)}</td>
                <td>{formatTokens(record.input_tokens, locale)}</td>
                <td>{formatTokens(record.cache_read_tokens, locale)}</td>
                <td>{formatPercent(record.hit_rate)}</td>
                <td>{formatDateTime(record.last_activity, locale)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  {#if cacheBreaks}
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.cacheBreaks')}
      </h3>
      <p class="stats-note">
        {t('statistics.usage.cacheBreaksSummary', {
          suspected: formatInteger(cacheBreaks.suspected_turns, locale),
          evaluated: formatInteger(cacheBreaks.evaluated_turns, locale),
        })}
        {t('statistics.usage.cacheBreaksHint')}
      </p>
      {#if cacheBreaks.incidents.length > 0}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
        <div
          class="stats-table-scroll"
          role="region"
          tabindex="0"
          aria-label={t('statistics.table.scroll')}
        >
          <table class="stats-table">
            <thead>
              <tr>
                <th>{t('statistics.col.time')}</th>
                <th>{t('statistics.col.agent')}</th>
                <th>{t('statistics.col.session')}</th>
                <th>{t('statistics.col.model')}</th>
                <th>{t('statistics.col.previousInput')}</th>
                <th>{t('statistics.col.cacheRead')}</th>
              </tr>
            </thead>
            <tbody>
              {#each cacheBreaks.incidents as incident (`${incident.session_id}:${incident.timestamp}`)}
                <tr>
                  <td>{formatDateTime(incident.timestamp, locale)}</td>
                  <td class="stats-mono"
                    >{@render agentName(incident.agent_id)}</td
                  >
                  <td class="stats-mono stats-truncate"
                    >{incident.session_id}</td
                  >
                  <td class="stats-mono">{incident.model}</td>
                  <td>{formatTokens(incident.previous_input_tokens, locale)}</td
                  >
                  <td>{formatTokens(incident.cache_read_tokens, locale)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </div>
  {/if}
</div>
