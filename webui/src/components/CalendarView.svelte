<script>
  import { provideAutosaveContext, useAutosaveContext } from '$lib/autosave.js';
  import CalendarEventDialogs from './calendar/CalendarEventDialogs.svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import {
    dayHeadingLabel,
    CALENDAR_VIEWS,
    weekdayLabels,
    formatTimeInZone,
    addDaysToKey,
    createCalendarController,
    createCalendarViewState,
    dayKeyForOccurrence,
    dayKeyToUtcDate,
    eventById,
    eventJobDueAt,
    eventJobs,
    groupByDay,
    isDayKey,
    monthGridDays,
    monthLabel,
    sortDayEntries,
    stepAnchor,
    todayKey,
    weekColumnLabel,
    weekRangeLabel,
    weekStartKey,
    windowForView,
  } from '$lib/calendarView.js';
  import { tooltip } from '$lib/tooltip.js';
  import TabList from './ui/TabList.svelte';
  import Banner from './ui/Banner.svelte';

  import { untrack } from 'svelte';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import { createCalendarEventEditor } from './calendar/editor.svelte.js';
  import { parseRrule } from '$lib/calendarEventForm.js';
  import { eventTimeLabel } from './cron/presentation.js';

  let {
    // The place is the shown period: [view mode, "YYYY-MM-DD" anchor day]. An
    // empty place shows the month around today. Layer toggles are not places.
    navigation = createStandaloneNavigation(),
    onToast = () => {},
    serverUnavailable = false,
    calendarRefreshToken = 0,
    // Bumped when the Cron jobs change; events show their Agent jobs.
    cronRefreshToken = 0,
    // Bumped when the Agents change; Agent jobs name their Agents.
    agentsRefreshToken = 0,
    onOpenCronJob = null,
    onOpenSession = null,
  } = $props();
  provideAutosaveContext(useAutosaveContext());
  let viewState = $state(createCalendarViewState());
  const controller = createCalendarController({ state: viewState });

  const editor = createCalendarEventEditor({
    get viewState() {
      return viewState;
    },
    get controller() {
      return controller;
    },
    get onToast() {
      return onToast;
    },
  });

  let lastRefreshToken = 0;
  let lastCronRefreshToken = 0;

  let locale = $derived(activeLocaleTag());
  let currentDayKey = $derived(todayKey(viewState.systemTimeZone));
  let gridDays = $derived(monthGridDays(viewState.anchorKey, currentDayKey));
  let anchorDate = $derived(dayKeyToUtcDate(viewState.anchorKey));
  let heading = $derived(
    monthLabel(anchorDate.getUTCFullYear(), anchorDate.getUTCMonth(), locale),
  );
  let localCount = $derived(viewState.occurrences.length);
  let cronCount = $derived(viewState.cron.length);

  let localByDay = $derived(
    viewState.showLocalLayer
      ? groupByDay(viewState.occurrences, (occurrence) =>
          dayKeyForOccurrence(occurrence, viewState.systemTimeZone),
        )
      : {},
  );
  let cronByDay = $derived(
    viewState.showCronLayer
      ? groupByDay(viewState.cron, (item) =>
          dayKeyForOccurrence(
            { all_day: false, start_utc: item.fire_at },
            viewState.systemTimeZone,
          ),
        )
      : {},
  );

  let weekColumns = $derived.by(() => {
    if (viewState.view !== 'week') {
      return [];
    }
    const start = weekStartKey(viewState.anchorKey);
    return Array.from({ length: 7 }, (_, index) => addDaysToKey(start, index));
  });

  let agendaDays = $derived.by(() => {
    if (viewState.view !== 'agenda') {
      return [];
    }
    const { from, to } = windowForView('agenda', viewState.anchorKey);
    const days = [];
    let cursor = from;
    while (cursor <= to) {
      days.push(cursor);
      cursor = addDaysToKey(cursor, 1);
    }
    return days;
  });

  let monthDayEntries = $derived.by(() => {
    const entries = {};
    for (const day of gridDays) {
      entries[day.key] = dayEntries(day.key);
    }
    return entries;
  });

  // Place -> shown period. A missing or unknown mode shows the month, a
  // missing or invalid day shows today, and the entry is corrected to the
  // concrete period once today is known in the server timezone.
  $effect(() => {
    const [requestedView = '', requestedAnchor = ''] = navigation.place;
    const zoneResolved = viewState.timeZoneResolved;
    untrack(() => {
      const view = CALENDAR_VIEWS.includes(requestedView)
        ? requestedView
        : CALENDAR_VIEWS[0];
      if (isDayKey(requestedAnchor)) {
        controller.show(view, requestedAnchor);
      } else if (zoneResolved) {
        controller.show(view, todayKey(viewState.systemTimeZone));
      } else {
        // Today is a guess until the first load reports the server timezone.
        controller.show(view, todayKey(), { today: true });
        return;
      }
      if (view !== requestedView || viewState.anchorKey !== requestedAnchor) {
        navigation.replace([view, viewState.anchorKey]);
      }
    });
  });

  // Every period change is a step.
  function showPeriod(view, anchorKey) {
    navigation.navigate([view, anchorKey]);
  }

  $effect(() => {
    const token = calendarRefreshToken;
    if (token === lastRefreshToken) {
      return;
    }
    lastRefreshToken = token;
    controller.load({ silent: true });
  });

  $effect(() => {
    const token = cronRefreshToken;
    if (token === lastCronRefreshToken) {
      return;
    }
    lastCronRefreshToken = token;
    controller.load({ silent: true });
  });

  function dayEntries(key) {
    const local = (localByDay[key] ?? []).map((occurrence) => ({
      kind: 'event',
      all_day: occurrence.all_day,
      start_utc: occurrence.start_utc,
      fire_at: '',
      title: occurrence.title,
      occurrence,
      event: eventById(viewState.events, occurrence.event_id),
    }));
    const cron = (cronByDay[key] ?? []).map((item) => ({
      kind: 'cron',
      all_day: false,
      start_utc: '',
      fire_at: item.fire_at,
      title: item.name,
      cron: item,
    }));
    return sortDayEntries([...local, ...cron]);
  }

  function occurrenceHeading(occurrence) {
    if (occurrence.all_day) {
      // A multi-day event names its first and last day.
      const first = String(occurrence.start).slice(0, 10);
      const last = addDaysToKey(String(occurrence.end).slice(0, 10), -1);
      return last > first
        ? t('calendar.detail.allDayRange', {
            range: new Intl.DateTimeFormat(locale, {
              day: 'numeric',
              month: 'short',
              timeZone: 'UTC',
            }).formatRange(dayKeyToUtcDate(first), dayKeyToUtcDate(last)),
          })
        : t('calendar.detail.allDay');
    }
    return `${formatTimeInZone(occurrence.start_utc, viewState.systemTimeZone, locale)} – ${formatTimeInZone(occurrence.end_utc, viewState.systemTimeZone, locale)}`;
  }

  function cronTimeLabel(cron) {
    return formatTimeInZone(cron.fire_at, viewState.systemTimeZone, locale);
  }

  // Each view steps by its own period; the arrows name it.
  const PREV_LABELS = {
    month: () => t('calendar.prevMonth'),
    week: () => t('calendar.prevWeek'),
    day: () => t('calendar.prevDay'),
    agenda: () => t('calendar.prevAgenda'),
  };
  const NEXT_LABELS = {
    month: () => t('calendar.nextMonth'),
    week: () => t('calendar.nextWeek'),
    day: () => t('calendar.nextDay'),
    agenda: () => t('calendar.nextAgenda'),
  };
  let prevLabel = $derived(
    (PREV_LABELS[viewState.view] ?? PREV_LABELS.month)(),
  );
  let nextLabel = $derived(
    (NEXT_LABELS[viewState.view] ?? NEXT_LABELS.month)(),
  );

  function entryTime(entry) {
    if (entry.kind === 'cron') {
      return cronTimeLabel(entry.cron);
    }
    return occurrenceHeading(entry.occurrence);
  }

  const FREQUENCY_LABELS = {
    daily: () => t('calendar.form.freqDaily'),
    weekly: () => t('calendar.form.freqWeekly'),
    monthly: () => t('calendar.form.freqMonthly'),
    yearly: () => t('calendar.form.freqYearly'),
  };

  // How the event's series repeats, as the form names its frequency.
  function recurrenceText(occurrence) {
    const event = eventById(viewState.events, occurrence.event_id);
    const freq = parseRrule(event?.rrule, String(event?.start ?? '')).freq;
    return FREQUENCY_LABELS[freq]?.() ?? t('calendar.detail.recurring');
  }

  // The Agent jobs of the event an occurrence belongs to.
  function occurrenceJobs(occurrence) {
    return eventJobs(viewState.jobs, occurrence.event_id);
  }

  // One Agent job as a details-card row: its time for this occurrence as the
  // label, then when it runs relative to the event, its Agent and
  // instruction. A job that is not active is muted.
  function jobRow(job, occurrence) {
    const due = eventJobDueAt(job, occurrence);
    const timing = eventTimeLabel(job.event_edge, job.event_offset_minutes);
    const prompt =
      job.prompt.length > 80 ? `${job.prompt.slice(0, 79)}…` : job.prompt;
    return {
      label: due
        ? formatTimeInZone(due.toISOString(), viewState.systemTimeZone, locale)
        : '',
      value: `${timing} · ${job.target}: ${prompt}`,
      tone: job.status === 'active' ? undefined : 'muted',
    };
  }

  // An entry's details card: its complete title, time with the zone it is
  // shown in, and what the chip leaves out (description, location,
  // repetition, Agent jobs; for a Schedule Run, that clicking opens the
  // Schedule).
  function entryDetails(entry) {
    const zone = viewState.systemTimeZone;
    if (entry.kind === 'cron') {
      return {
        title: entry.cron.name,
        text: t('calendar.details.cronLead'),
        rows: [
          { label: t('calendar.details.time'), value: entryTime(entry) },
          { label: t('calendar.details.timeZone'), value: zone },
        ],
      };
    }
    const occurrence = entry.occurrence;
    const jobs = occurrenceJobs(occurrence);
    return {
      title: entry.title,
      text: occurrence.description || '',
      rows: [
        { label: t('calendar.details.time'), value: entryTime(entry) },
        {
          label: t('calendar.details.timeZone'),
          value: occurrence.all_day ? '' : zone,
        },
        {
          label: t('calendar.form.location'),
          value: occurrence.location || '',
        },
        {
          label: t('calendar.form.recurrence'),
          value: occurrence.recurring ? recurrenceText(occurrence) : '',
        },
        {
          label: t('calendar.jobs.heading'),
          value: jobs.length ? String(jobs.length) : '',
        },
        ...jobs.map((job) => jobRow(job, occurrence)),
      ],
    };
  }

  // The entries a month cell has no room for, as time and title rows.
  function moreDetails(entries) {
    return {
      title: t('calendar.details.more'),
      rows: entries.map((entry) => ({
        label: entryTime(entry),
        value: entry.kind === 'cron' ? entry.cron.name : entry.title,
      })),
    };
  }

  // A layer chip: what the layer holds, how many entries this view has, and
  // whether it is shown.
  function layerDetails(layer) {
    const shown =
      layer === 'local' ? viewState.showLocalLayer : viewState.showCronLayer;
    return {
      title:
        layer === 'local'
          ? t('calendar.layer.local')
          : t('calendar.layer.cron'),
      text:
        layer === 'local'
          ? t('calendar.layer.localHint')
          : t('calendar.layer.cronHint'),
      rows: [
        {
          label: t('calendar.layer.inView'),
          value: String(layer === 'local' ? localCount : cronCount),
        },
        {
          value: shown ? t('calendar.layer.shown') : t('calendar.layer.hidden'),
          tone: shown ? undefined : 'muted',
        },
      ],
    };
  }
</script>

<!-- An event's Agent job count, keyed to the Schedules layer color. The
     details card lists the jobs, so the marker itself is not announced. -->
{#snippet jobMarker(occurrence)}
  {@const count = occurrenceJobs(occurrence).length}
  {#if count > 0}
    <span class="calendar-entry-jobs" aria-hidden="true">
      <svg viewBox="0 0 12 12" width="10" height="10">
        <path d="M6.5 1 2.5 7h3l-1 4 4-6h-3z" />
      </svg>{count}
    </span>
  {/if}
{/snippet}

<div class="view-frame calendar-view">
  <header class="view-header">
    <div class="view-header__intro">
      <h1 class="view-header__title">{t('calendar.title')}</h1>
      <p class="view-header__subtitle">
        {t('calendar.subtitle')}
      </p>
    </div>
  </header>
  <div class="view-toolbar view-toolbar--stack">
    <div class="calendar-toolbar-row">
      <div class="calendar-nav">
        <Button
          variant="secondary"
          icon
          onClick={() =>
            showPeriod(
              viewState.view,
              stepAnchor(viewState.view, viewState.anchorKey, -1),
            )}
          ariaLabel={prevLabel}
          tooltip={prevLabel}
        >
          ‹
        </Button>
        <Button
          variant="secondary"
          onClick={() =>
            showPeriod(viewState.view, todayKey(viewState.systemTimeZone))}
        >
          {t('calendar.today')}
        </Button>
        <Button
          variant="secondary"
          icon
          onClick={() =>
            showPeriod(
              viewState.view,
              stepAnchor(viewState.view, viewState.anchorKey, 1),
            )}
          ariaLabel={nextLabel}
          tooltip={nextLabel}
        >
          ›
        </Button>
        <span class="calendar-heading">
          {#if viewState.view === 'day'}
            {dayHeadingLabel(viewState.anchorKey, locale)}
          {:else if viewState.view === 'week'}
            {weekRangeLabel(viewState.anchorKey, locale)}
          {:else if viewState.view === 'agenda'}
            {t('calendar.agendaHeading')}
          {:else}
            {heading}
          {/if}
        </span>
      </div>
      <div class="calendar-toolbar-right">
        <div class="calendar-layers">
          <button
            type="button"
            class="calendar-chip calendar-chip--local"
            class:is-off={!viewState.showLocalLayer}
            aria-pressed={viewState.showLocalLayer}
            onclick={() => controller.toggleLayer('local')}
            use:tooltip={() => layerDetails('local')}
          >
            {t('calendar.layer.local')}
            <span class="calendar-chip-count">{localCount}</span>
          </button>
          <button
            type="button"
            class="calendar-chip calendar-chip--cron"
            class:is-off={!viewState.showCronLayer}
            aria-pressed={viewState.showCronLayer}
            onclick={() => controller.toggleLayer('cron')}
            use:tooltip={() => layerDetails('cron')}
          >
            {t('calendar.layer.cron')}
            <span class="calendar-chip-count">{cronCount}</span>
          </button>
        </div>
        <Button variant="primary" onClick={() => editor.openCreate()}>
          {t('calendar.newEvent')}
        </Button>
      </div>
    </div>
    <TabList
      items={CALENDAR_VIEWS.map((view) => ({
        id: view,
        label: t(`calendar.view.${view}`),
      }))}
      value={viewState.view}
      onChange={(view) => showPeriod(view, viewState.anchorKey)}
      appearance="segmented"
      density="compact"
    />
  </div>

  {#if viewState.loadError}
    <Banner variant="error">
      <span
        >{t('calendar.loadError')}
        {viewState.loadError}</span
      >
      <Button variant="secondary" onClick={() => controller.load()}>
        {t('common.retry')}
      </Button>
    </Banner>
  {:else if serverUnavailable}
    <Banner variant="warn">
      {t('calendar.serverUnavailable')}
    </Banner>
  {:else if viewState.loading}
    <p class="calendar-loading">{t('calendar.loading')}</p>
  {:else if viewState.view === 'month'}
    <div class="calendar-grid" role="grid">
      <div class="calendar-weekdays">
        {#each weekdayLabels(locale) as label (label)}
          <span class="calendar-weekday">{label}</span>
        {/each}
      </div>
      {#each gridDays as day (day.key)}
        {@const dayEntriesList = monthDayEntries[day.key] ?? []}
        <div
          class="calendar-cell"
          class:is-outside={!day.inMonth}
          class:is-today={day.isToday}
        >
          <button
            type="button"
            class="calendar-cell-surface"
            aria-label={t('calendar.addOnDay')}
            onclick={() => editor.openCreate(day.key)}
          >
            <span class="calendar-day-number">{day.dayOfMonth}</span>
            <span class="calendar-cell-add" aria-hidden="true">＋</span>
          </button>
          <div class="calendar-cell-entries">
            {#each dayEntriesList.slice(0, 4) as entry, index (index)}
              {#if entry.kind === 'cron'}
                <button
                  type="button"
                  class="calendar-entry calendar-entry--cron"
                  use:tooltip={() => entryDetails(entry)}
                  onclick={(event) => {
                    event.stopPropagation();
                    onOpenCronJob?.(entry.cron.job_id);
                  }}
                >
                  <span class="calendar-entry-time"
                    >{cronTimeLabel(entry.cron)}</span
                  >
                  <span class="calendar-entry-title">{entry.cron.name}</span>
                </button>
              {:else}
                <button
                  type="button"
                  class="calendar-entry"
                  use:tooltip={() => entryDetails(entry)}
                  class:calendar-entry--allday={entry.all_day}
                  onclick={(event) => {
                    event.stopPropagation();
                    editor.openDetail(entry.occurrence);
                  }}
                >
                  {#if !entry.all_day}
                    <span class="calendar-entry-time">
                      {formatTimeInZone(
                        entry.start_utc,
                        viewState.systemTimeZone,
                        locale,
                      )}
                    </span>
                  {/if}
                  <span class="calendar-entry-title">{entry.title}</span>
                  {#if entry.occurrence?.recurring}
                    <span class="calendar-entry-repeat" aria-hidden="true"
                      >↻</span
                    >
                  {/if}
                  {@render jobMarker(entry.occurrence)}
                </button>
              {/if}
            {/each}
            {#if dayEntriesList.length > 4}
              <span
                class="calendar-entry-more"
                use:tooltip={() => moreDetails(dayEntriesList.slice(4))}
                >+{dayEntriesList.length - 4}</span
              >
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {:else if viewState.view === 'week'}
    <div class="calendar-columns">
      {#each weekColumns as dayKey (dayKey)}
        {@const entries = dayEntries(dayKey)}
        {@const columnLabel = weekColumnLabel(dayKey, locale)}
        <section
          class="calendar-column"
          class:is-today={dayKey === currentDayKey}
        >
          <h2
            class="calendar-column-heading"
            aria-label={dayHeadingLabel(dayKey, locale)}
          >
            <span class="calendar-column-weekday">{columnLabel.weekday}</span>
            <span class="calendar-day-number">{columnLabel.dayOfMonth}</span>
          </h2>
          <div class="calendar-column-entries">
            {#each entries as entry, index (index)}
              {#if entry.kind === 'cron'}
                <button
                  type="button"
                  class="calendar-entry calendar-entry--cron"
                  use:tooltip={() => entryDetails(entry)}
                  onclick={() => onOpenCronJob?.(entry.cron.job_id)}
                >
                  <span class="calendar-entry-time"
                    >{cronTimeLabel(entry.cron)}</span
                  >
                  <span class="calendar-entry-title">{entry.cron.name}</span>
                </button>
              {:else}
                <button
                  type="button"
                  class="calendar-entry"
                  use:tooltip={() => entryDetails(entry)}
                  class:calendar-entry--allday={entry.all_day}
                  onclick={() => editor.openDetail(entry.occurrence)}
                >
                  {#if !entry.all_day}
                    <span class="calendar-entry-time">
                      {formatTimeInZone(
                        entry.start_utc,
                        viewState.systemTimeZone,
                        locale,
                      )}
                    </span>
                  {/if}
                  <span class="calendar-entry-title">{entry.title}</span>
                  {#if entry.occurrence?.recurring}
                    <span class="calendar-entry-repeat" aria-hidden="true"
                      >↻</span
                    >
                  {/if}
                  {@render jobMarker(entry.occurrence)}
                </button>
              {/if}
            {:else}
              <button
                type="button"
                class="calendar-column-add"
                onclick={() => editor.openCreate(dayKey)}
              >
                {t('calendar.addOnDay')}
              </button>
            {/each}
          </div>
        </section>
      {/each}
    </div>
  {:else if viewState.view === 'day'}
    <div class="calendar-columns calendar-columns--single">
      <section
        class="calendar-column"
        aria-label={dayHeadingLabel(viewState.anchorKey, locale)}
      >
        <div class="calendar-column-entries">
          {#each dayEntries(viewState.anchorKey) as entry, index (index)}
            {#if entry.kind === 'cron'}
              <button
                type="button"
                class="calendar-entry calendar-entry--cron"
                use:tooltip={() => entryDetails(entry)}
                onclick={() => onOpenCronJob?.(entry.cron.job_id)}
              >
                <span class="calendar-entry-time"
                  >{cronTimeLabel(entry.cron)}</span
                >
                <span class="calendar-entry-title">{entry.cron.name}</span>
              </button>
            {:else}
              <button
                type="button"
                class="calendar-entry"
                use:tooltip={() => entryDetails(entry)}
                class:calendar-entry--allday={entry.all_day}
                onclick={() => editor.openDetail(entry.occurrence)}
              >
                {#if !entry.all_day}
                  <span class="calendar-entry-time">
                    {formatTimeInZone(
                      entry.start_utc,
                      viewState.systemTimeZone,
                      locale,
                    )}
                  </span>
                {/if}
                <span class="calendar-entry-title">{entry.title}</span>
                {#if entry.occurrence?.recurring}
                  <span class="calendar-entry-repeat" aria-hidden="true">↻</span
                  >
                {/if}
                {@render jobMarker(entry.occurrence)}
              </button>
            {/if}
          {:else}
            <button
              type="button"
              class="calendar-column-add"
              onclick={() => editor.openCreate()}
            >
              {t('calendar.addOnDay')}
            </button>
          {/each}
        </div>
      </section>
    </div>
  {:else}
    <div class="calendar-agenda">
      {#each agendaDays as dayKey (dayKey)}
        {@const entries = dayEntries(dayKey)}
        {#if dayKey === currentDayKey || entries.length > 0}
          <section class="calendar-agenda-day">
            <h2 class="calendar-agenda-heading">
              {dayHeadingLabel(dayKey, locale)}
              {#if dayKey === currentDayKey}
                <span class="calendar-agenda-today">{t('calendar.today')}</span>
              {/if}
            </h2>
            {#if entries.length === 0}
              <p class="calendar-agenda-free">
                {t('calendar.freeDay')}
              </p>
            {:else}
              <ul class="calendar-agenda-list">
                {#each entries as entry, index (index)}
                  <li>
                    {#if entry.kind === 'cron'}
                      <button
                        type="button"
                        class="calendar-entry calendar-entry--cron"
                        use:tooltip={() => entryDetails(entry)}
                        onclick={() => onOpenCronJob?.(entry.cron.job_id)}
                      >
                        <span class="calendar-entry-time"
                          >{cronTimeLabel(entry.cron)}</span
                        >
                        <span class="calendar-entry-title"
                          >{entry.cron.name}</span
                        >
                      </button>
                    {:else}
                      <button
                        type="button"
                        class="calendar-entry"
                        use:tooltip={() => entryDetails(entry)}
                        class:calendar-entry--allday={entry.all_day}
                        onclick={() => editor.openDetail(entry.occurrence)}
                      >
                        {#if !entry.all_day}
                          <span class="calendar-entry-time">
                            {formatTimeInZone(
                              entry.start_utc,
                              viewState.systemTimeZone,
                              locale,
                            )}
                          </span>
                        {:else}
                          <span class="calendar-entry-time"
                            >{t('calendar.detail.allDay')}</span
                          >
                        {/if}
                        <span class="calendar-entry-title">{entry.title}</span>
                        {@render jobMarker(entry.occurrence)}
                      </button>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
          </section>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<CalendarEventDialogs
  {editor}
  {viewState}
  {controller}
  {serverUnavailable}
  {agentsRefreshToken}
  {onOpenSession}
  {occurrenceHeading}
  {recurrenceText}
  {occurrenceJobs}
/>
