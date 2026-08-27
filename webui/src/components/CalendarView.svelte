<script>
  import { onMount } from 'svelte';

  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import TabList from './ui/TabList.svelte';
  import TextArea from './ui/TextArea.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    CALENDAR_VIEWS,
    createCalendarController,
    createCalendarViewState,
    dayHeadingLabel,
    dayKeyForOccurrence,
    dayKeyToUtcDate,
    eventById,
    eventToFormValues,
    formatTimeInZone,
    groupByDay,
    monthGridDays,
    monthLabel,
    sortDayEntries,
    todayKey,
    weekdayLabels,
    weekStartKey,
    windowForView,
  } from '$lib/calendarView.js';

  let {
    onToast = () => {},
    serverUnavailable = false,
    calendarRefreshToken = 0,
    onOpenCronJob = null,
  } = $props();

  let viewState = $state(createCalendarViewState());
  const controller = createCalendarController({ state: viewState, onToast });

  const EMPTY_FORM = () => ({
    title: '',
    notes: '',
    all_day: false,
    start_date: todayKey(),
    start_time: '09:00',
    duration_minutes: 60,
    duration_days: 1,
    freq: 'none',
    interval: 1,
    by_weekday: ['mo', 'tu', 'we', 'th', 'fr'],
    end_mode: 'never',
    end_count: 10,
    end_until: '',
  });

  let lastRefreshToken = 0;

  let formOpen = $state(false);
  let formMode = $state('create');
  let formEventId = $state('');
  let formValues = $state(EMPTY_FORM());
  let formError = $state('');
  let submitting = $state(false);

  let detailOpen = $state(false);
  let detailOccurrence = $state(null);

  let deleteTarget = $state(null);
  let deleteOccurrenceOnly = $state(false);

  let locale = $derived(activeLocaleTag());
  let gridDays = $derived(monthGridDays(viewState.anchorKey));
  let anchorDate = $derived(dayKeyToUtcDate(viewState.anchorKey));
  let heading = $derived(
    monthLabel(anchorDate.getUTCFullYear(), anchorDate.getUTCMonth(), locale),
  );
  let localCount = $derived(viewState.occurrences.length);
  let cronCount = $derived(viewState.cron.length);
  let hasAnyContent = $derived(
    (viewState.showLocalLayer && localCount > 0) ||
      (viewState.showCronLayer && cronCount > 0),
  );

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
            { all_day: false, start_utc: item.fire_at, start_date: '' },
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
    return Array.from({ length: 7 }, (_, index) => addDaysSafe(start, index));
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
      cursor = addDaysSafe(cursor, 1);
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

  onMount(() => {
    controller.load();
  });

  $effect(() => {
    const token = calendarRefreshToken;
    if (token === lastRefreshToken) {
      return;
    }
    lastRefreshToken = token;
    controller.load({ silent: true });
  });

  function addDaysSafe(key, days) {
    const utc = dayKeyToUtcDate(key).getTime() + days * 24 * 60 * 60 * 1000;
    return new Date(utc).toISOString().slice(0, 10);
  }

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

  function openCreate(dayKey = viewState.anchorKey) {
    formMode = 'create';
    formEventId = '';
    formValues = { ...EMPTY_FORM(), start_date: dayKey };
    formError = '';
    formOpen = true;
  }

  function openDetail(occurrence) {
    detailOccurrence = occurrence;
    detailOpen = true;
  }

  function openEdit(occurrence) {
    const event = eventById(viewState.events, occurrence.event_id);
    if (!event) {
      return;
    }
    formMode = 'edit';
    formEventId = event.id;
    formValues = eventToFormValues(event);
    formError = '';
    detailOpen = false;
    formOpen = true;
  }

  async function submitForm() {
    if (!formValues.title.trim()) {
      formError = t(
        'calendar.errors.titleRequired',
        'Please give the event a title.',
      );
      return;
    }
    submitting = true;
    formError = '';
    try {
      const payload = formValuesToEventPayload(formValues);
      if (formMode === 'edit') {
        await controller.updateEvent(formEventId, payload);
      } else {
        await controller.createEvent(payload);
      }
      formOpen = false;
    } catch (error) {
      formError = error?.message ?? String(error);
    } finally {
      submitting = false;
    }
  }

  function requestDelete(occurrence) {
    detailOpen = false;
    deleteOccurrenceOnly = false;
    deleteTarget = occurrence;
  }

  async function confirmDelete() {
    const occurrence = deleteTarget;
    deleteTarget = null;
    if (!occurrence) {
      return;
    }
    try {
      if (occurrence.recurring && deleteOccurrenceOnly) {
        await controller.excludeOccurrence(
          occurrence.event_id,
          occurrenceExdateValue(occurrence),
        );
      } else {
        await controller.deleteEvent(occurrence.event_id);
      }
    } catch (error) {
      onToast(error?.message ?? String(error));
    }
  }

  // The exclusion (RFC 5545 EXDATE) uses the event's own start form: a naive
  // local datetime for timed events, a plain date for all-day events. The
  // server renders it per occurrence in the event's anchor zone.
  function occurrenceExdateValue(occurrence) {
    return occurrence.occurrence_start;
  }

  function occurrenceHeading(occurrence) {
    if (occurrence.all_day) {
      return t('calendar.detail.allDay', 'All day');
    }
    return `${formatTimeInZone(occurrence.start_utc, viewState.systemTimeZone, locale)} – ${formatTimeInZone(occurrence.end_utc, viewState.systemTimeZone, locale)}`;
  }

  function cronTimeLabel(cron) {
    return formatTimeInZone(cron.fire_at, viewState.systemTimeZone, locale);
  }

  function formValuesToEventPayload(values) {
    const payload = {
      title: values.title,
      notes: values.notes || null,
      all_day: values.all_day,
    };
    if (values.all_day) {
      payload.start = values.start_date;
      payload.duration_days = Number(values.duration_days) || 1;
    } else {
      payload.start = `${values.start_date}T${values.start_time || '09:00'}:00`;
      payload.duration_minutes = Number(values.duration_minutes) || 60;
    }
    if (values.freq !== 'none') {
      const rrule = {
        freq: values.freq,
        interval: Number(values.interval) || 1,
      };
      if (values.freq === 'weekly') {
        rrule.by_weekday = values.by_weekday?.length
          ? values.by_weekday
          : ['mo'];
      }
      if (values.end_mode === 'count') {
        rrule.count = Number(values.end_count) || 10;
      } else if (values.end_mode === 'until') {
        rrule.until = values.end_until || undefined;
      }
      payload.rrule = rrule;
    } else {
      payload.rrule = null;
    }
    return payload;
  }
</script>

<div class="view-frame calendar-view">
  <header class="view-header">
    <div class="view-header-text">
      <h1 class="view-header-title">{t('calendar.title', 'Calendar')}</h1>
      <p class="view-header-subtitle">
        {t(
          'calendar.subtitle',
          'Your appointments and the agent schedule in one view.',
        )}
      </p>
    </div>
  </header>
  <div class="view-toolbar view-toolbar--stack">
    <div class="calendar-toolbar-row">
      <div class="calendar-nav">
        <Button
          variant="secondary"
          icon
          onClick={() => controller.navigate(-1)}
          tooltip={t('calendar.prev', 'Previous period')}
        >
          ‹
        </Button>
        <Button variant="secondary" onClick={() => controller.goToday()}>
          {t('calendar.today', 'Today')}
        </Button>
        <Button
          variant="secondary"
          icon
          onClick={() => controller.navigate(1)}
          tooltip={t('calendar.next', 'Next period')}
        >
          ›
        </Button>
        <span class="calendar-heading">
          {#if viewState.view === 'day'}
            {dayHeadingLabel(viewState.anchorKey, locale)}
          {:else if viewState.view === 'agenda'}
            {t('calendar.agendaHeading', 'Next two weeks')}
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
            onclick={() => controller.toggleLayer('local')}
            use:tooltip={t(
              'calendar.layer.localHint',
              'Appointments stored in vBot',
            )}
          >
            {t('calendar.layer.local', 'Events')}
            <span class="calendar-chip-count">{localCount}</span>
          </button>
          <button
            type="button"
            class="calendar-chip calendar-chip--cron"
            class:is-off={!viewState.showCronLayer}
            onclick={() => controller.toggleLayer('cron')}
            use:tooltip={t(
              'calendar.layer.cronHint',
              'Scheduled agent runs, shown from the schedule only.',
            )}
          >
            {t('calendar.layer.cron', 'Cron')}
            <span class="calendar-chip-count">{cronCount}</span>
          </button>
        </div>
        <Button variant="primary" onClick={() => openCreate()}>
          {t('calendar.newEvent', 'New event')}
        </Button>
      </div>
    </div>
    <TabList
      items={CALENDAR_VIEWS.map((view) => ({
        id: view,
        label: t(`calendar.view.${view}`, view),
      }))}
      value={viewState.view}
      onChange={(view) => controller.setView(view)}
      appearance="segmented"
      density="compact"
    />
  </div>

  {#if viewState.loadError}
    <Banner variant="error">
      <span
        >{t('calendar.loadError', 'The calendar could not be loaded.')}
        {viewState.loadError}</span
      >
      <Button variant="secondary" onClick={() => controller.load()}>
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {:else if serverUnavailable}
    <Banner variant="warn">
      {t(
        'calendar.serverUnavailable',
        'The vBot server is not reachable right now.',
      )}
    </Banner>
  {:else if viewState.loading}
    <p class="calendar-loading">{t('calendar.loading', 'Loading calendar…')}</p>
  {:else if !hasAnyContent}
    <EmptyState
      title={t('calendar.emptyTitle', 'Nothing scheduled')}
      description={t(
        'calendar.emptyDescription',
        'Create an event yourself, or ask the agent to add one for you.',
      )}
    >
      {#snippet actions()}
        <Button variant="primary" onClick={() => openCreate()}>
          {t('calendar.newEvent', 'New event')}
        </Button>
      {/snippet}
    </EmptyState>
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
            class="calendar-day-number"
            onclick={() => openCreate(day.key)}
            use:tooltip={t('calendar.addOnDay', 'Add an event on this day')}
          >
            {day.dayOfMonth}
          </button>
          <div class="calendar-cell-entries">
            {#each dayEntriesList.slice(0, 4) as entry, index (index)}
              {#if entry.kind === 'cron'}
                <button
                  type="button"
                  class="calendar-entry calendar-entry--cron"
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
                  class:calendar-entry--allday={entry.all_day}
                  onclick={() => openDetail(entry.occurrence)}
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
                </button>
              {/if}
            {/each}
            {#if dayEntriesList.length > 4}
              <span class="calendar-entry-more"
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
        <section class="calendar-column" class:is-today={dayKey === todayKey()}>
          <h2 class="calendar-column-heading">
            {dayHeadingLabel(dayKey, locale)}
          </h2>
          <div class="calendar-column-entries">
            {#each entries as entry, index (index)}
              {#if entry.kind === 'cron'}
                <button
                  type="button"
                  class="calendar-entry calendar-entry--cron"
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
                  class:calendar-entry--allday={entry.all_day}
                  onclick={() => openDetail(entry.occurrence)}
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
                </button>
              {/if}
            {:else}
              <button
                type="button"
                class="calendar-column-add"
                onclick={() => openCreate(dayKey)}
              >
                {t('calendar.addOnDay', 'Add an event on this day')}
              </button>
            {/each}
          </div>
        </section>
      {/each}
    </div>
  {:else if viewState.view === 'day'}
    <div class="calendar-columns calendar-columns--single">
      <section class="calendar-column">
        <h2 class="calendar-column-heading">
          {dayHeadingLabel(viewState.anchorKey, locale)}
        </h2>
        <div class="calendar-column-entries">
          {#each dayEntries(viewState.anchorKey) as entry, index (index)}
            {#if entry.kind === 'cron'}
              <button
                type="button"
                class="calendar-entry calendar-entry--cron"
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
                class:calendar-entry--allday={entry.all_day}
                onclick={() => openDetail(entry.occurrence)}
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
              </button>
            {/if}
          {:else}
            <button
              type="button"
              class="calendar-column-add"
              onclick={() => openCreate()}
            >
              {t('calendar.addOnDay', 'Add an event on this day')}
            </button>
          {/each}
        </div>
      </section>
    </div>
  {:else}
    <div class="calendar-agenda">
      {#each agendaDays as dayKey (dayKey)}
        {@const entries = dayEntries(dayKey)}
        {#if dayKey === todayKey() || entries.length > 0}
          <section class="calendar-agenda-day">
            <h2 class="calendar-agenda-heading">
              {dayHeadingLabel(dayKey, locale)}
              {#if dayKey === todayKey()}
                <span class="calendar-agenda-today"
                  >{t('calendar.today', 'Today')}</span
                >
              {/if}
            </h2>
            {#if entries.length === 0}
              <p class="calendar-agenda-free">
                {t('calendar.freeDay', 'Nothing scheduled.')}
              </p>
            {:else}
              <ul class="calendar-agenda-list">
                {#each entries as entry, index (index)}
                  <li>
                    {#if entry.kind === 'cron'}
                      <button
                        type="button"
                        class="calendar-entry calendar-entry--cron"
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
                        class:calendar-entry--allday={entry.all_day}
                        onclick={() => openDetail(entry.occurrence)}
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
                            >{t('calendar.detail.allDay', 'All day')}</span
                          >
                        {/if}
                        <span class="calendar-entry-title">{entry.title}</span>
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

{#if formOpen}
  <Modal
    title={formMode === 'edit'
      ? t('calendar.form.editTitle', 'Edit event')
      : t('calendar.form.createTitle', 'New event')}
    labelledById="calendar-form-title"
    onClose={() => (formOpen = false)}
  >
    {#snippet body()}
      <div class="modal-body">
        <form
          class="calendar-form"
          id="calendar-event-form"
          onsubmit={(event) => {
            event.preventDefault();
            submitForm();
          }}
        >
          <FormField
            label={t('calendar.form.title', 'Title')}
            controlId="calendar-form-title-input"
          >
            <TextField
              id="calendar-form-title-input"
              value={formValues.title}
              onInput={(next) => (formValues.title = next)}
              placeholder={t(
                'calendar.form.titlePlaceholder',
                'Dentist appointment',
              )}
            />
          </FormField>
          <div class="calendar-form-row">
            <FormField
              label={t('calendar.form.date', 'Date')}
              controlId="calendar-form-date"
            >
              <TextField
                id="calendar-form-date"
                type="date"
                value={formValues.start_date}
                onInput={(next) => (formValues.start_date = next)}
                ariaLabel={t('calendar.form.date', 'Date')}
              />
            </FormField>
            {#if !formValues.all_day}
              <FormField
                label={t('calendar.form.time', 'Start')}
                controlId="calendar-form-time"
              >
                <TextField
                  id="calendar-form-time"
                  type="time"
                  value={formValues.start_time}
                  onInput={(next) => (formValues.start_time = next)}
                  ariaLabel={t('calendar.form.time', 'Start')}
                />
              </FormField>
              <FormField
                label={t('calendar.form.duration', 'Duration (minutes)')}
                controlId="calendar-form-duration"
                full
              >
                <TextField
                  id="calendar-form-duration"
                  type="number"
                  value={formValues.duration_minutes}
                  onInput={(next) => (formValues.duration_minutes = next)}
                  min="5"
                  step="5"
                  ariaLabel={t('calendar.form.duration', 'Duration (minutes)')}
                />
              </FormField>
            {:else}
              <FormField
                label={t('calendar.form.days', 'Days')}
                controlId="calendar-form-days"
              >
                <TextField
                  id="calendar-form-days"
                  type="number"
                  value={formValues.duration_days}
                  onInput={(next) => (formValues.duration_days = next)}
                  min="1"
                  ariaLabel={t('calendar.form.days', 'Days')}
                />
              </FormField>
            {/if}
          </div>
          <div class="calendar-form-toggle">
            <span>{t('calendar.form.allDay', 'All day')}</span>
            <Toggle
              checked={formValues.all_day}
              onChange={(next) => (formValues.all_day = next)}
              size="sm"
              ariaLabel={t('calendar.form.allDay', 'All day')}
            />
          </div>
          <FormField
            label={t('calendar.form.recurrence', 'Repeats')}
            controlId="calendar-form-freq"
          >
            <select
              id="calendar-form-freq"
              class="s-input"
              bind:value={formValues.freq}
            >
              <option value="none"
                >{t('calendar.form.freqNone', 'Not repeating')}</option
              >
              <option value="daily"
                >{t('calendar.form.freqDaily', 'Daily')}</option
              >
              <option value="weekly"
                >{t('calendar.form.freqWeekly', 'Weekly')}</option
              >
              <option value="monthly"
                >{t('calendar.form.freqMonthly', 'Monthly')}</option
              >
              <option value="yearly"
                >{t('calendar.form.freqYearly', 'Yearly')}</option
              >
            </select>
          </FormField>
          {#if formValues.freq !== 'none'}
            <FormField
              label={t('calendar.form.interval', 'Every')}
              controlId="calendar-form-interval"
            >
              <TextField
                id="calendar-form-interval"
                type="number"
                value={formValues.interval}
                onInput={(next) => (formValues.interval = next)}
                min="1"
                ariaLabel={t('calendar.form.interval', 'Every')}
              />
            </FormField>
            {#if formValues.freq === 'weekly'}
              <FormField
                label={t('calendar.form.weekdays', 'On days')}
                controlId="calendar-form-weekdays"
              >
                <div
                  class="calendar-weekday-picker"
                  id="calendar-form-weekdays"
                >
                  {#each [['mo', 'Mo'], ['tu', 'Tu'], ['we', 'We'], ['th', 'Th'], ['fr', 'Fr'], ['sa', 'Sa'], ['su', 'Su']] as [code, label] (code)}
                    <button
                      type="button"
                      class="calendar-weekday-option"
                      class:is-active={formValues.by_weekday.includes(code)}
                      onclick={() => {
                        formValues.by_weekday = formValues.by_weekday.includes(
                          code,
                        )
                          ? formValues.by_weekday.filter((day) => day !== code)
                          : [...formValues.by_weekday, code];
                      }}
                    >
                      {label}
                    </button>
                  {/each}
                </div>
              </FormField>
            {/if}
            <FormField
              label={t('calendar.form.ends', 'Ends')}
              controlId="calendar-form-end-mode"
            >
              <div class="calendar-form-ends">
                <select
                  id="calendar-form-end-mode"
                  class="s-input"
                  bind:value={formValues.end_mode}
                >
                  <option value="never"
                    >{t('calendar.form.endsNever', 'Never')}</option
                  >
                  <option value="count"
                    >{t('calendar.form.endsCount', 'After')}</option
                  >
                  <option value="until"
                    >{t('calendar.form.endsUntil', 'On date')}</option
                  >
                </select>
                {#if formValues.end_mode === 'count'}
                  <TextField
                    type="number"
                    value={formValues.end_count}
                    onInput={(next) => (formValues.end_count = next)}
                    min="1"
                    ariaLabel={t('calendar.form.endsCount', 'After')}
                  />
                  <span class="calendar-form-ends-unit"
                    >{t('calendar.form.times', 'times')}</span
                  >
                {:else if formValues.end_mode === 'until'}
                  <TextField
                    type="date"
                    value={formValues.end_until}
                    onInput={(next) => (formValues.end_until = next)}
                    ariaLabel={t('calendar.form.endsUntil', 'On date')}
                  />
                {/if}
              </div>
            </FormField>
          {/if}
          <FormField
            label={t('calendar.form.notes', 'Notes')}
            controlId="calendar-form-notes"
          >
            <TextArea
              id="calendar-form-notes"
              value={formValues.notes}
              onInput={(next) => (formValues.notes = next)}
              rows={3}
              placeholder={t('calendar.form.notesPlaceholder', 'Optional')}
            />
          </FormField>
          {#if formError}
            <Banner variant="error">{formError}</Banner>
          {/if}
        </form>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={() => (formOpen = false)}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button variant="primary" onClick={submitForm} disabled={submitting}>
        {formMode === 'edit'
          ? t('common.save', 'Save')
          : t('calendar.form.create', 'Create event')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if detailOpen && detailOccurrence}
  <Modal
    title={detailOccurrence.title}
    labelledById="calendar-detail-title"
    onClose={() => (detailOpen = false)}
  >
    {#snippet body()}
      <div class="modal-body">
        <div class="calendar-detail">
          <p class="calendar-detail-time">
            {occurrenceHeading(detailOccurrence)}
          </p>
          {#if detailOccurrence.notes}
            <p class="calendar-detail-notes">{detailOccurrence.notes}</p>
          {/if}
          {#if detailOccurrence.recurring}
            <p class="calendar-detail-meta">
              {t('calendar.detail.recurring', 'Repeating event')}
            </p>
          {/if}
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={() => openEdit(detailOccurrence)}>
        {t('common.edit', 'Edit')}
      </Button>
      <Button variant="danger" onClick={() => requestDelete(detailOccurrence)}>
        {t('common.delete', 'Delete')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if deleteTarget}
  <ConfirmDialog
    title={t('calendar.deleteTitle', 'Delete event')}
    body={deleteTarget.recurring
      ? t(
          'calendar.deleteBody',
          'This event repeats. You can delete the whole series or only this occurrence.',
        )
      : t(
          'calendar.deleteBodySingle',
          'This removes the event from your calendar.',
        )}
    confirmLabel={deleteTarget.recurring && deleteOccurrenceOnly
      ? t('calendar.deleteOccurrence', 'Only this occurrence')
      : t('calendar.deleteSeries', 'Delete event')}
    cancelLabel={t('common.cancel', 'Cancel')}
    danger={true}
    onConfirm={confirmDelete}
    onCancel={() => (deleteTarget = null)}
  />
  {#if deleteTarget.recurring}
    <div
      class="calendar-delete-choice"
      role="radiogroup"
      aria-label={t('calendar.deleteScope', 'What to delete')}
    >
      <label>
        <input type="radio" bind:group={deleteOccurrenceOnly} value={false} />
        {t('calendar.deleteSeries', 'Delete event')}
      </label>
      <label>
        <input type="radio" bind:group={deleteOccurrenceOnly} value={true} />
        {t('calendar.deleteOccurrence', 'Only this occurrence')}
      </label>
    </div>
  {/if}
{/if}
