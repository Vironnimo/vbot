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
        {t('statistics.usage.measuredTokens', 'Measured tokens')}
      </h3>
      <div class="stats-grid stats-grid--three">
        {@render statCard(
          t('statistics.col.input', 'Input'),
          formatTokens(usage.totals.measured_input_tokens, locale),
        )}{@render statCard(
          t('statistics.col.output', 'Output'),
          formatTokens(usage.totals.measured_output_tokens, locale),
        )}{@render statCard(
          t('statistics.usage.measuredTurns', 'Measured Model calls'),
          formatInteger(usage.totals.measured_turns, locale),
        )}
      </div>
    </div>
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.estimatedTokens', 'Estimated tokens')}<InfoHint
          text={t(
            'statistics.estimatedHint',
            'Estimated tokens are approximated, not provider-reported.',
          )}
        />
      </h3>
      <div class="stats-grid stats-grid--three">
        {@render statCard(
          t('statistics.col.input', 'Input'),
          formatTokens(usage.totals.estimated_input_tokens, locale),
        )}{@render statCard(
          t('statistics.col.output', 'Output'),
          formatTokens(usage.totals.estimated_output_tokens, locale),
        )}{@render statCard(
          t('statistics.usage.estimatedTurns', 'Estimated Model calls'),
          formatInteger(usage.totals.estimated_turns, locale),
        )}
      </div>
    </div>
  </div>
  {#if usage.kinds?.length}
    <div class="stats-block">
      <h3 class="stats-block__title">
        {t('statistics.usage.byKind', 'Model calls by activity')}
      </h3>
      <div class="stats-table-wrap">
        <table
          class="stats-table"
          aria-label={t('statistics.usage.byKind', 'Model calls by activity')}
        >
          <thead
            ><tr>
              <th>{t('statistics.col.activity', 'Activity')}</th>
              <th class="num">{t('statistics.col.calls', 'Calls')}</th>
              <th class="num"
                >{t(
                  'statistics.usage.incomplete',
                  'Incomplete token usage',
                )}</th
              >
            </tr></thead
          >
          <tbody>
            {#each usage.kinds as row (row.kind)}
              <tr>
                <td>{modelCallKindLabel(row.kind, t)}</td>
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
      {t('statistics.usage.cacheAndReasoning', 'Cache & Reasoning')}<InfoHint
        text={t(
          'statistics.usage.reasoningHint',
          'Reasoning tokens are provider-reported subsets of measured output and are never added to token totals.',
        )}
      />
    </h3>
    <div class="stats-grid">
      {@render statCard(
        t('statistics.usage.cacheHitRate', 'Cache hit rate'),
        formatPercent(cacheHitRate(usage.totals)),
        t(
          'statistics.usage.cacheHitHint',
          'Cache hit rate: tokens read from cache as a share of the input, over the turns that report cache data.',
        ),
      )}{@render statCard(
        t('statistics.usage.cacheRead', 'Cache read'),
        usage.totals.cache_turns > 0
          ? formatTokens(usage.totals.cache_read_tokens, locale)
          : '—',
      )}{@render statCard(
        t('statistics.usage.cacheWrite', 'Cache write'),
        usage.totals.cache_turns > 0
          ? formatTokens(usage.totals.cache_write_tokens, locale)
          : '—',
      )}{@render statCard(
        t('statistics.usage.reasoning', 'Reasoning (output subset)'),
        formatReasoningTokens(usage.totals),
      )}
    </div>
  </div>
  <CostsPanel {report} />
  <div class="stats-block__head">
    <h3 class="stats-section-title">
      {t('statistics.usage.breakdown', 'Usage by Provider & Model')}
    </h3>
    <InfoHint
      text={t(
        'statistics.usage.runAttributionHint',
        'Provider and Model Run counts mean “involved in this Run.” A fallback Run can appear in multiple rows, and Model duration is the full Run duration.',
      )}
    />
  </div>
  <div class="stats-block">
    <h3 class="stats-block__title">
      {t('statistics.usage.providers', 'Providers')}
    </h3>
    {#if usage.providers.length === 0}
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
        aria-label={t(
          'statistics.table.scroll',
          'Statistics table; scroll for more columns',
        )}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.provider', 'Provider')}</th>
              <th>{t('statistics.col.runs', 'Runs')}</th>
              <th>{t('statistics.col.tokens', 'Tokens')}</th>
              <th>{t('statistics.col.reasoning', 'Reasoning')}</th>
              <th>{t('statistics.col.cacheHit', 'Cache hit')}</th>
              <th>{t('statistics.col.share', 'Share')}</th>
              <th>{t('statistics.col.errors', 'Errors')}</th>
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
      {t('statistics.usage.models', 'Models')}
    </h3>
    {#each providerGroups as group (group.provider)}
      <h4 class="stats-subheading stats-mono">{group.provider}</h4>
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t(
          'statistics.table.scroll',
          'Statistics table; scroll for more columns',
        )}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.model', 'Model')}</th>
              <th>{t('statistics.col.runs', 'Runs')}</th>
              <th>{t('statistics.col.tokens', 'Tokens')}</th>
              <th>{t('statistics.col.reasoning', 'Reasoning')}</th>
              <th>{t('statistics.col.cacheHit', 'Cache hit')}</th>
              <th>{t('statistics.col.avgDuration', 'Avg')}</th>
              <th>{t('statistics.col.errors', 'Errors')}</th>
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
      {t(
        'statistics.usage.cacheSessions',
        'Sessions with lowest cache hit rate',
      )}
    </h3>
    {#if cacheSessions.length === 0}
      <EmptyState
        density="compact"
        description={t(
          'statistics.usage.cacheEmpty',
          'No cache-reporting activity yet.',
        )}
      />
    {:else}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
      <div
        class="stats-table-scroll"
        role="region"
        tabindex="0"
        aria-label={t(
          'statistics.table.scroll',
          'Statistics table; scroll for more columns',
        )}
      >
        <table class="stats-table">
          <thead>
            <tr>
              <th>{t('statistics.col.agent', 'Agent')}</th>
              <th>{t('statistics.col.session', 'Session')}</th>
              <th>{t('statistics.col.turns', 'Turns')}</th>
              <th>{t('statistics.col.input', 'Input')}</th>
              <th>{t('statistics.col.cacheRead', 'Cache read')}</th>
              <th>{t('statistics.col.hitRate', 'Hit rate')}</th>
              <th>{t('statistics.col.lastActivity', 'Last activity')}</th>
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
        {t('statistics.usage.cacheBreaks', 'Suspected cache breaks (derived)')}
      </h3>
      <p class="stats-note">
        {t(
          'statistics.usage.cacheBreaksSummary',
          '{suspected} suspected breaks across {evaluated} evaluated continuation turns.',
          {
            suspected: formatInteger(cacheBreaks.suspected_turns, locale),
            evaluated: formatInteger(cacheBreaks.evaluated_turns, locale),
          },
        )}
        {t(
          'statistics.usage.cacheBreaksHint',
          'A turn whose cache read fell far below the previous prompt although nothing legitimate explains a miss (new session, compaction, takeover, model switch, expired cache, or a tiny prompt are excluded). Best-effort heuristic, not authoritative.',
        )}
      </p>
      {#if cacheBreaks.incidents.length > 0}
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
        <div
          class="stats-table-scroll"
          role="region"
          tabindex="0"
          aria-label={t(
            'statistics.table.scroll',
            'Statistics table; scroll for more columns',
          )}
        >
          <table class="stats-table">
            <thead>
              <tr>
                <th>{t('statistics.col.time', 'Time')}</th>
                <th>{t('statistics.col.agent', 'Agent')}</th>
                <th>{t('statistics.col.session', 'Session')}</th>
                <th>{t('statistics.col.model', 'Model')}</th>
                <th>{t('statistics.col.previousInput', 'Prev. input')}</th>
                <th>{t('statistics.col.cacheRead', 'Cache read')}</th>
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
