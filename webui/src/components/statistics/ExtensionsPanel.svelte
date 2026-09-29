<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import Badge from '../ui/Badge.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatCost,
    formatDateTime,
    formatInteger,
    shortGroupId,
    tokenBreakdownTooltip,
    tokenSplit,
    unfinishedRunsTooltip,
  } from '$lib/statisticsView.js';
  import { statCard, tokenCell, tokensHeader } from './ReportPrimitives.svelte';

  let { report } = $props();

  const locale = $derived(activeLocaleTag());
  const extensions = $derived(report?.extensions?.extensions ?? []);

  // Groups started before generated titles existed carry none; name them by
  // start time and a short id so they stay distinguishable.
  function groupLabel(group) {
    if (group.title) return group.title;
    const id = shortGroupId(group.group_id);
    if (!group.started_at) {
      return t('statistics.extensions.groupFallbackId', { id });
    }
    return t('statistics.extensions.groupFallback', {
      date: formatDateTime(group.started_at, locale),
      id,
    });
  }

  function failedRuns(activity) {
    const status = activity.run_status;
    return status.failed + status.cancelled + status.interrupted;
  }

  function groupSummary(group) {
    return t('statistics.extensions.groupSummary', {
      participants: formatInteger(group.participants.length, locale),
      runs: formatInteger(group.activity.runs, locale),
    });
  }
</script>

{#snippet activityCards(activity, groups)}
  <div class="stats-grid">
    {#if groups != null}
      {@render statCard(
        t('statistics.extensions.groups'),
        formatInteger(groups, locale),
        t('statistics.extensions.groupsHint'),
      )}
    {/if}
    {@render statCard(
      t('statistics.extensions.sessions'),
      formatInteger(activity.sessions, locale),
    )}
    {@render statCard(
      t('statistics.extensions.runs'),
      formatInteger(activity.runs, locale),
      null,
      t('statistics.extensions.unfinishedRuns', {
        count: formatInteger(failedRuns(activity), locale),
      }),
      failedRuns(activity) > 0
        ? unfinishedRunsTooltip(activity.run_status, locale)
        : '',
    )}
    {@render statCard(
      t('statistics.extensions.tokens'),
      formatInteger(tokenSplit(activity).measured, locale),
      null,
      tokenSplit(activity).hasEstimated
        ? t('statistics.extensions.estimatedTokens', {
            count: formatInteger(tokenSplit(activity).estimated, locale),
          })
        : null,
      tokenSplit(activity).hasEstimated
        ? tokenBreakdownTooltip(activity, locale)
        : '',
    )}
    {@render statCard(
      t('statistics.cost.reported'),
      formatCost(activity.costs.reported_usd, locale),
      null,
      t('statistics.cost.callCount', {
        count: formatInteger(activity.costs.reported_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.estimated'),
      formatCost(activity.costs.estimated_usd, locale),
      null,
      t('statistics.cost.callCount', {
        count: formatInteger(activity.costs.estimated_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.extensions.toolCalls'),
      formatInteger(activity.tool_calls, locale),
    )}
  </div>
{/snippet}

<div class="stats-panel">
  <p class="stats-note">
    {t('statistics.extensions.note')}
  </p>
  {#if extensions.length === 0}
    <EmptyState
      density="compact"
      description={t('statistics.extensions.empty')}
    />
  {/if}
  {#each extensions as extension (extension.name)}
    <section class="stats-block stats-extension">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          <span class="stats-mono">{extension.name}</span>
          <Badge variant="neutral">{t('statistics.agent.extensionBadge')}</Badge
          >
        </h3>
      </div>
      {@render activityCards(extension.activity, extension.total_groups)}
      {#if extension.groups_truncated}
        <p class="stats-note stats-spaced">
          {t('statistics.extensions.truncated', {
            shown: formatInteger(extension.groups.length, locale),
            total: formatInteger(extension.total_groups, locale),
          })}
        </p>
      {/if}
      <div class="stats-call-list">
        {#each extension.groups as group (group.group_id)}
          <details class="stats-call stats-extension-group">
            <summary>
              <span class="stats-call__time"
                >{formatDateTime(group.started_at, locale)}</span
              >
              <span class="stats-wrap">{groupLabel(group)}</span>
              <span class="stats-call__source">{groupSummary(group)}</span>
              <strong>{@render tokenCell(group.activity, false)}</strong>
            </summary>
            <div class="stats-call__body">
              {@render activityCards(group.activity, null)}
              <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
              <div
                class="stats-table-scroll stats-spaced"
                role="region"
                tabindex="0"
                aria-label={t('statistics.extensions.participants')}
              >
                <table class="stats-table">
                  <thead>
                    <tr>
                      <th>{t('statistics.extensions.participant')}</th>
                      <th>{t('statistics.col.model')}</th>
                      <th>{t('statistics.extensions.runs')}</th>
                      <th
                        >{@render tokensHeader(
                          t('statistics.extensions.tokens'),
                        )}</th
                      >
                      <th>{t('statistics.cost.reported')}</th>
                      <th>{t('statistics.cost.estimated')}</th>
                      <th>{t('statistics.extensions.toolCalls')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each group.participants as participant (participant.session_id)}
                      <tr>
                        <td>{participant.name || participant.participant_id}</td
                        >
                        <td class="stats-mono stats-wrap"
                          >{participant.model ?? '—'}</td
                        >
                        <td
                          >{formatInteger(
                            participant.activity.runs,
                            locale,
                          )}</td
                        >
                        <td>{@render tokenCell(participant.activity)}</td>
                        <td
                          >{formatCost(
                            participant.activity.costs.reported_usd,
                            locale,
                          )}</td
                        >
                        <td
                          >{formatCost(
                            participant.activity.costs.estimated_usd,
                            locale,
                          )}</td
                        >
                        <td
                          >{formatInteger(
                            participant.activity.tool_calls,
                            locale,
                          )}</td
                        >
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </div>
          </details>
        {/each}
      </div>
    </section>
  {/each}
</div>
