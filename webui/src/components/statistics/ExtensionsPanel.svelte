<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import Badge from '../ui/Badge.svelte';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    formatCost,
    formatDateTime,
    formatInteger,
    shortGroupId,
    tokenSplit,
  } from '$lib/statisticsView.js';
  import { statCard, tokenCell } from './ReportPrimitives.svelte';

  let { report } = $props();

  const locale = $derived(activeLocaleTag());
  const extensions = $derived(report?.extensions?.extensions ?? []);

  // Groups started before generated titles existed carry none; name them by
  // start time and a short id so they stay distinguishable.
  function groupLabel(group) {
    if (group.title) return group.title;
    const id = shortGroupId(group.group_id);
    if (!group.started_at) {
      return t('statistics.extensions.groupFallbackId', 'Group {id}', { id });
    }
    return t('statistics.extensions.groupFallback', 'Started {date} · {id}', {
      date: formatDateTime(group.started_at, locale),
      id,
    });
  }

  function failedRuns(activity) {
    const status = activity.run_status;
    return status.failed + status.cancelled + status.interrupted;
  }

  function groupSummary(group) {
    return t(
      'statistics.extensions.groupSummary',
      '{participants} participants · {runs} Runs',
      {
        participants: formatInteger(group.participants.length, locale),
        runs: formatInteger(group.activity.runs, locale),
      },
    );
  }
</script>

{#snippet activityCards(activity, groups)}
  <div class="stats-grid">
    {#if groups != null}
      {@render statCard(
        t('statistics.extensions.groups', 'Groups'),
        formatInteger(groups, locale),
        t(
          'statistics.extensions.groupsHint',
          'A group is one unit of work the Extension started, such as one Swarm, with its participant Sessions.',
        ),
      )}
    {/if}
    {@render statCard(
      t('statistics.extensions.sessions', 'Participant Sessions'),
      formatInteger(activity.sessions, locale),
    )}
    {@render statCard(
      t('statistics.extensions.runs', 'Runs'),
      formatInteger(activity.runs, locale),
      null,
      t('statistics.extensions.unfinishedRuns', '{count} not completed', {
        count: formatInteger(failedRuns(activity), locale),
      }),
    )}
    {@render statCard(
      t('statistics.extensions.tokens', 'Tokens'),
      formatInteger(tokenSplit(activity).measured, locale),
      null,
      tokenSplit(activity).hasEstimated
        ? t('statistics.extensions.estimatedTokens', '+{count} estimated', {
            count: formatInteger(tokenSplit(activity).estimated, locale),
          })
        : null,
    )}
    {@render statCard(
      t('statistics.cost.reported', 'Provider-reported cost'),
      formatCost(activity.costs.reported_usd, locale),
      null,
      t('statistics.cost.callCount', '{count} calls', {
        count: formatInteger(activity.costs.reported_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.cost.estimated', 'Estimated API value'),
      formatCost(activity.costs.estimated_usd, locale),
      null,
      t('statistics.cost.callCount', '{count} calls', {
        count: formatInteger(activity.costs.estimated_calls, locale),
      }),
    )}
    {@render statCard(
      t('statistics.extensions.toolCalls', 'Tool calls'),
      formatInteger(activity.tool_calls, locale),
    )}
  </div>
{/snippet}

<div class="stats-panel">
  <p class="stats-note">
    {t(
      'statistics.extensions.note',
      'Extensions such as Swarm run participant Sessions of their own. Every other tab already includes this activity under the Extension’s name; this tab breaks it down by group and participant.',
    )}
  </p>
  {#if extensions.length === 0}
    <EmptyState
      density="compact"
      description={t(
        'statistics.extensions.empty',
        'No Extension activity in this time range.',
      )}
    />
  {/if}
  {#each extensions as extension (extension.name)}
    <section class="stats-block stats-extension">
      <div class="stats-block__head">
        <h3 class="stats-block__title">
          <span class="stats-mono">{extension.name}</span>
          <Badge variant="neutral"
            >{t('statistics.agent.extensionBadge', 'Extension')}</Badge
          >
        </h3>
      </div>
      {@render activityCards(extension.activity, extension.total_groups)}
      {#if extension.groups_truncated}
        <p class="stats-note stats-spaced">
          {t(
            'statistics.extensions.truncated',
            'Showing the {shown} most recently active of {total} groups.',
            {
              shown: formatInteger(extension.groups.length, locale),
              total: formatInteger(extension.total_groups, locale),
            },
          )}
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
              <strong>{@render tokenCell(group.activity)}</strong>
            </summary>
            <div class="stats-call__body">
              {@render activityCards(group.activity, null)}
              <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users scroll wide tables here.) -->
              <div
                class="stats-table-scroll stats-spaced"
                role="region"
                tabindex="0"
                aria-label={t(
                  'statistics.extensions.participants',
                  'Participants',
                )}
              >
                <table class="stats-table">
                  <thead>
                    <tr>
                      <th
                        >{t(
                          'statistics.extensions.participant',
                          'Participant',
                        )}</th
                      >
                      <th>{t('statistics.col.model', 'Model')}</th>
                      <th>{t('statistics.extensions.runs', 'Runs')}</th>
                      <th>{t('statistics.extensions.tokens', 'Tokens')}</th>
                      <th
                        >{t(
                          'statistics.cost.reported',
                          'Provider-reported cost',
                        )}</th
                      >
                      <th
                        >{t(
                          'statistics.cost.estimated',
                          'Estimated API value',
                        )}</th
                      >
                      <th
                        >{t(
                          'statistics.extensions.toolCalls',
                          'Tool calls',
                        )}</th
                      >
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
