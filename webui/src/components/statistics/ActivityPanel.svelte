<script>
  // The Overview's hero: the user's days with vBot over the last year, their
  // streaks, and a few personal highlights, from the all-time `activity`
  // section. It sits above the range toolbar because it ignores the range.
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import { dateKeyInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import EmptyState from '../ui/EmptyState.svelte';
  import {
    ACTIVITY_METRICS,
    dateFromKey,
    daysBetween,
    firstWeekday,
    formatCost,
    formatDurationMs,
    formatHourLabel,
    formatInteger,
    formatPercent,
    formatShortDateTime,
    formatTokens,
    peakIndex,
    tokenTooltip,
  } from '$lib/statisticsView.js';
  import ActivityHeatmap from './ActivityHeatmap.svelte';
  import AgentName from './report/AgentName.svelte';
  import ModelName from './report/ModelName.svelte';

  let { section, metric = $bindable('runs') } = $props();

  // War and Peace in tokens; the comparison only needs the order of magnitude.
  const WAR_AND_PEACE_TOKENS = 730_000;

  const locale = $derived(activeLocaleTag());
  const today = $derived(dateKeyInApplicationZone());
  const totals = $derived(section.totals ?? {});
  const streaks = $derived(section.streaks ?? {});
  const records = $derived(section.records ?? {});
  const hasActivity = $derived(
    (section.days ?? []).some((day) => day.runs > 0 || day.tokens > 0),
  );
  const dayCount = $derived(daysBetween(totals.first_active, today));
  const peakHour = $derived(peakIndex(section.by_hour));
  const peakWeekday = $derived(peakIndex(section.by_weekday));
  const hourMax = $derived(Math.max(0, ...(section.by_hour ?? [])));
  const books = $derived(
    Math.floor((totals.tokens ?? 0) / WAR_AND_PEACE_TOKENS),
  );

  // A day as `Oct 16`, with its year outside the current one.
  function formatDay(key, options = null) {
    const date = dateFromKey(key);
    if (!date) return '—';
    const shown = options ?? {
      month: 'short',
      day: 'numeric',
      ...(key.slice(0, 4) === today.slice(0, 4) ? {} : { year: 'numeric' }),
    };
    return new Intl.DateTimeFormat(locale, {
      ...shown,
      timeZone: 'UTC',
    }).format(date);
  }

  function days(count) {
    return count === 1
      ? t('statistics.activity.days.one')
      : t('statistics.activity.days.many', {
          count: formatInteger(count, locale),
        });
  }

  function runsCount(count) {
    return count === 1
      ? t('statistics.activity.runsCount.one')
      : t('statistics.activity.runsCount.many', {
          count: formatInteger(count, locale),
        });
  }

  // ISO weekday index (0 Monday) as the locale's weekday name.
  function weekdayName(index) {
    // 2024-01-01 was a Monday.
    return formatDay(`2024-01-0${index + 1}`, { weekday: 'long' });
  }

  function metricLabel(id) {
    return t(`statistics.activity.metric.${id}`);
  }

  function cellValueText(cell) {
    if (metric === 'tokens') return formatTokens(cell.value, locale);
    if (metric === 'cost') return formatCost(cell.day?.cost_usd ?? 0, locale);
    return runsCount(cell.value);
  }

  function cellLabel(cell) {
    return `${formatDay(cell.date, { dateStyle: 'full' })}: ${cellValueText(cell)}`;
  }

  function cellTooltip(cell) {
    const title = formatDay(cell.date, { dateStyle: 'full' });
    const day = cell.day;
    if (!day || (day.runs === 0 && day.tokens === 0)) {
      return { title, text: t('statistics.activity.noActivity') };
    }
    const rows = [
      {
        label: t('statistics.activity.yourRuns'),
        value: formatInteger(day.your_runs, locale),
      },
      {
        label: t('statistics.activity.messages'),
        value: formatInteger(day.messages, locale),
      },
    ];
    if (day.runs > day.your_runs) {
      rows.push({
        label: t('statistics.activity.agentRuns'),
        value: formatInteger(day.runs - day.your_runs, locale),
      });
    }
    rows.push(
      {
        label: t('statistics.activity.metric.tokens'),
        value: formatTokens(day.tokens, locale),
      },
      {
        label: t('statistics.activity.metric.cost'),
        value: formatCost(day.cost_usd, locale),
      },
    );
    return { title, rows };
  }

  const streakTiles = $derived([
    {
      id: 'current',
      label: t('statistics.activity.currentStreak'),
      value: days(streaks.current ?? 0),
      detail:
        streaks.current > 0
          ? t('statistics.activity.since', {
              date: formatDay(streaks.current_start),
            })
          : t('statistics.activity.streakIdle'),
      accent: streaks.current > 0,
    },
    {
      id: 'longest',
      label: t('statistics.activity.longestStreak'),
      value: days(streaks.longest ?? 0),
      detail:
        streaks.longest > 0
          ? `${formatDay(streaks.longest_start)} – ${formatDay(streaks.longest_end)}`
          : '',
    },
    {
      id: 'active',
      label: t('statistics.activity.activeDays'),
      value: formatInteger(totals.active_days ?? 0, locale),
      detail:
        dayCount !== null
          ? t('statistics.activity.ofDays', {
              share: formatPercent(
                (totals.active_days ?? 0) / dayCount,
                locale,
                0,
              ),
              total: formatInteger(dayCount, locale),
            })
          : '',
    },
    {
      id: 'busiest',
      label: t('statistics.activity.busiestDay'),
      value: records.busiest_day
        ? runsCount(records.busiest_day.your_runs)
        : '—',
      detail: records.busiest_day ? formatDay(records.busiest_day.date) : '',
    },
  ]);
</script>

<section
  class="stats-block stats-activity"
  aria-labelledby="stats-activity-title"
>
  <div class="stats-activity__head">
    <div class="stats-activity__intro">
      <h3 class="stats-activity__title" id="stats-activity-title">
        {t('statistics.activity.title')}
      </h3>
      {#if hasActivity && dayCount !== null}
        <p class="stats-activity__subtitle">
          {t('statistics.activity.dayWithVbot', {
            day: formatInteger(dayCount, locale),
            date: formatDay(totals.first_active, { dateStyle: 'medium' }),
          })}
        </p>
      {/if}
    </div>
    {#if hasActivity}
      <div
        class="stats-toggle"
        role="group"
        aria-label={t('statistics.activity.metricLabel')}
      >
        {#each ACTIVITY_METRICS as id (id)}
          <button
            type="button"
            class="stats-toggle__option"
            class:stats-toggle__option--active={metric === id}
            aria-pressed={metric === id}
            onclick={() => (metric = id)}>{metricLabel(id)}</button
          >
        {/each}
      </div>
    {/if}
  </div>

  {#if !hasActivity}
    <EmptyState
      density="compact"
      description={t('statistics.activity.empty')}
    />
  {:else}
    <div class="stats-activity__body">
      <ActivityHeatmap
        days={section.days}
        {metric}
        {today}
        {locale}
        weekStart={firstWeekday(locale)}
        ariaLabel={t('statistics.activity.calendarLabel', {
          metric: metricLabel(metric),
        })}
        {cellLabel}
        {cellTooltip}
      />
      <dl class="stats-activity__streaks">
        {#each streakTiles as tile (tile.id)}
          <div
            class="stats-activity__streak"
            class:stats-activity__streak--accent={tile.accent}
          >
            <dt>{tile.label}</dt>
            <dd class="stats-activity__streak-value">{tile.value}</dd>
            {#if tile.detail}
              <dd class="stats-activity__streak-detail">{tile.detail}</dd>
            {/if}
          </div>
        {/each}
      </dl>
    </div>

    <ul
      class="stats-highlights"
      aria-label={t('statistics.activity.highlights')}
    >
      <li class="stats-highlight">
        <span class="stats-highlight__label"
          >{t('statistics.activity.favoriteModel')}</span
        >
        <span class="stats-highlight__value">
          {#if section.favorite_model}
            <ModelName id={section.favorite_model.model} />
          {:else}
            —
          {/if}
        </span>
        {#if section.favorite_model?.share != null}
          <span class="stats-highlight__detail"
            >{t('statistics.activity.tokenShare', {
              share: formatPercent(section.favorite_model.share, locale, 0),
            })}</span
          >
        {/if}
      </li>
      <li class="stats-highlight">
        <span class="stats-highlight__label"
          >{t('statistics.activity.favoriteAgent')}</span
        >
        <span class="stats-highlight__value">
          {#if section.favorite_agent}
            <AgentName agentId={section.favorite_agent.agent_id} />
          {:else}
            —
          {/if}
        </span>
        {#if section.favorite_agent?.share != null}
          <span class="stats-highlight__detail"
            >{t('statistics.activity.runShare', {
              share: formatPercent(section.favorite_agent.share, locale, 0),
            })}</span
          >
        {/if}
      </li>
      <li class="stats-highlight">
        <span class="stats-highlight__label"
          >{t('statistics.activity.peakTime')}</span
        >
        <span class="stats-highlight__value"
          >{peakHour === null
            ? '—'
            : `${formatHourLabel(peakHour)}–${formatHourLabel((peakHour + 1) % 24)}`}</span
        >
        {#if hourMax > 0}
          <span
            class="stats-highlight__hours"
            aria-hidden="true"
            use:tooltip={t('statistics.activity.hoursHint')}
          >
            {#each section.by_hour as count, hour (hour)}
              <span
                class:stats-highlight__hour--peak={hour === peakHour}
                style:height={`${Math.max(8, (count / hourMax) * 100)}%`}
                style:opacity={count > 0 ? 1 : 0.35}
              ></span>
            {/each}
          </span>
        {/if}
        {#if peakWeekday !== null}
          <span class="stats-highlight__detail"
            >{t('statistics.activity.peakWeekday', {
              weekday: weekdayName(peakWeekday),
            })}</span
          >
        {/if}
      </li>
      <li class="stats-highlight">
        <span class="stats-highlight__label"
          >{t('statistics.activity.messagesSent')}</span
        >
        <span class="stats-highlight__value"
          >{formatInteger(totals.messages ?? 0, locale)}</span
        >
        <span class="stats-highlight__detail"
          >{t('statistics.activity.messagesDetail', {
            runs: formatInteger(totals.your_runs ?? 0, locale),
            sessions: formatInteger(totals.sessions ?? 0, locale),
          })}</span
        >
      </li>
      <li class="stats-highlight">
        <span class="stats-highlight__label"
          >{t('statistics.activity.longestRun')}</span
        >
        <span class="stats-highlight__value"
          >{records.longest_run
            ? formatDurationMs(records.longest_run.duration_ms)
            : '—'}</span
        >
        {#if records.longest_run}
          <span class="stats-highlight__detail">
            <AgentName agentId={records.longest_run.agent_id} />
            · {formatShortDateTime(records.longest_run.started_at, locale)}
          </span>
        {/if}
      </li>
      <li class="stats-highlight">
        <span class="stats-highlight__label"
          >{t('statistics.activity.tokensAllTime')}</span
        >
        <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users reach the exact count here.) -->
        <span
          class="stats-highlight__value"
          tabindex="0"
          use:tooltip={tokenTooltip(totals.tokens, locale)}
          >{formatTokens(totals.tokens ?? 0, locale)}</span
        >
        <span class="stats-highlight__detail"
          >{books >= 1
            ? books === 1
              ? t('statistics.activity.books.one')
              : t('statistics.activity.books.many', {
                  count: formatInteger(books, locale),
                })
            : t('statistics.activity.toolCalls', {
                value: formatInteger(totals.tool_calls ?? 0, locale),
              })}</span
        >
      </li>
    </ul>
  {/if}
</section>
