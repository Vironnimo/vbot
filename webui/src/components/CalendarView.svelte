<script>
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
    groupByDay,
    monthGridDays,
    monthLabel,
    sortDayEntries,
    todayKey,
    weekStartKey,
    windowForView,
  } from '$lib/calendarView.js';
  import { tooltip } from '$lib/tooltip.js';
  import TabList from './ui/TabList.svelte';
  import Banner from './ui/Banner.svelte';
  import Modal from './ui/Modal.svelte';
  import FormField from './ui/FormField.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import TextArea from './ui/TextArea.svelte';
  import CalendarActions from './CalendarActions.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onMount } from 'svelte';
  import { createCalendarEventEditor } from './calendar/editor.svelte.js';

  let {
    onToast = () => {},
    serverUnavailable = false,
    calendarRefreshToken = 0,
    onOpenCronJob = null,
    onOpenSession = null,
  } = $props();
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
      return t('calendar.detail.allDay', 'All day');
    }
    return `${formatTimeInZone(occurrence.start_utc, viewState.systemTimeZone, locale)} – ${formatTimeInZone(occurrence.end_utc, viewState.systemTimeZone, locale)}`;
  }

  function cronTimeLabel(cron) {
    return formatTimeInZone(cron.fire_at, viewState.systemTimeZone, locale);
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
        <Button variant="primary" onClick={() => editor.openCreate()}>
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

  {#if viewState.actionError}
    <Banner variant="error">{viewState.actionError}</Banner>
  {/if}
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
            aria-label={t('calendar.addOnDay', 'Add an event on this day')}
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
        <section
          class="calendar-column"
          class:is-today={dayKey === currentDayKey}
        >
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
                </button>
              {/if}
            {:else}
              <button
                type="button"
                class="calendar-column-add"
                onclick={() => editor.openCreate(dayKey)}
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
              </button>
            {/if}
          {:else}
            <button
              type="button"
              class="calendar-column-add"
              onclick={() => editor.openCreate()}
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
        {#if dayKey === currentDayKey || entries.length > 0}
          <section class="calendar-agenda-day">
            <h2 class="calendar-agenda-heading">
              {dayHeadingLabel(dayKey, locale)}
              {#if dayKey === currentDayKey}
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

{#if editor.formOpen}
  <Modal
    title={editor.formMode === 'edit'
      ? t('calendar.form.editTitle', 'Edit event')
      : t('calendar.form.createTitle', 'New event')}
    labelledById="calendar-form-title"
    onClose={() => (editor.formOpen = false)}
  >
    {#snippet body()}
      <div class="modal-body">
        <form
          class="calendar-form"
          id="calendar-event-form"
          onsubmit={(event) => {
            event.preventDefault();
            editor.submitForm();
          }}
        >
          <FormField
            label={t('calendar.form.title', 'Title')}
            controlId="calendar-form-title-input"
          >
            <TextField
              id="calendar-form-title-input"
              value={editor.formValues.title}
              onInput={(next) => (editor.formValues.title = next)}
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
                value={editor.formValues.start_date}
                onInput={(next) => (editor.formValues.start_date = next)}
                ariaLabel={t('calendar.form.date', 'Date')}
              />
            </FormField>
            {#if !editor.formValues.all_day}
              <FormField
                label={t('calendar.form.time', 'Start')}
                controlId="calendar-form-time"
              >
                <TextField
                  id="calendar-form-time"
                  type="time"
                  value={editor.formValues.start_time}
                  onInput={(next) => (editor.formValues.start_time = next)}
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
                  value={editor.formValues.duration_minutes}
                  onInput={(next) =>
                    (editor.formValues.duration_minutes = next)}
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
                  value={editor.formValues.duration_days}
                  onInput={(next) => (editor.formValues.duration_days = next)}
                  min="1"
                  ariaLabel={t('calendar.form.days', 'Days')}
                />
              </FormField>
            {/if}
          </div>
          <div class="calendar-form-toggle">
            <span>{t('calendar.form.allDay', 'All day')}</span>
            <Toggle
              checked={editor.formValues.all_day}
              onChange={(next) => (editor.formValues.all_day = next)}
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
              bind:value={editor.formValues.freq}
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
          {#if editor.formValues.freq !== 'none'}
            <FormField
              label={t('calendar.form.interval', 'Every')}
              controlId="calendar-form-interval"
            >
              <TextField
                id="calendar-form-interval"
                type="number"
                value={editor.formValues.interval}
                onInput={(next) => (editor.formValues.interval = next)}
                min="1"
                ariaLabel={t('calendar.form.interval', 'Every')}
              />
            </FormField>
            {#if editor.formValues.freq === 'weekly'}
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
                      class:is-active={editor.formValues.by_weekday.includes(
                        code,
                      )}
                      onclick={() => {
                        editor.formValues.by_weekday =
                          editor.formValues.by_weekday.includes(code)
                            ? editor.formValues.by_weekday.filter(
                                (day) => day !== code,
                              )
                            : [...editor.formValues.by_weekday, code];
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
                  bind:value={editor.formValues.end_mode}
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
                {#if editor.formValues.end_mode === 'count'}
                  <TextField
                    type="number"
                    value={editor.formValues.end_count}
                    onInput={(next) => (editor.formValues.end_count = next)}
                    min="1"
                    ariaLabel={t('calendar.form.endsCount', 'After')}
                  />
                  <span class="calendar-form-ends-unit"
                    >{t('calendar.form.times', 'times')}</span
                  >
                {:else if editor.formValues.end_mode === 'until'}
                  <TextField
                    type="date"
                    value={editor.formValues.end_until}
                    onInput={(next) => (editor.formValues.end_until = next)}
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
              value={editor.formValues.notes}
              onInput={(next) => (editor.formValues.notes = next)}
              rows={3}
              placeholder={t('calendar.form.notesPlaceholder', 'Optional')}
            />
          </FormField>
          {#if editor.formError}
            <Banner variant="error">{editor.formError}</Banner>
          {/if}
        </form>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={() => (editor.formOpen = false)}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="primary"
        onClick={editor.submitForm}
        disabled={editor.submitting}
      >
        {editor.formMode === 'edit'
          ? t('common.save', 'Save')
          : t('calendar.form.create', 'Create event')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if editor.detailOpen && editor.detailOccurrence}
  <Modal
    title={editor.detailOccurrence.title}
    labelledById="calendar-detail-title"
    onClose={() => (editor.detailOpen = false)}
  >
    {#snippet body()}
      <div class="modal-body">
        <div class="calendar-detail">
          <p class="calendar-detail-time">
            {occurrenceHeading(editor.detailOccurrence)}
          </p>
          {#if editor.detailOccurrence.notes}
            <p class="calendar-detail-notes">{editor.detailOccurrence.notes}</p>
          {/if}
          {#if editor.detailOccurrence.recurring}
            <p class="calendar-detail-meta">
              {t('calendar.detail.recurring', 'Repeating event')}
            </p>
          {/if}
          <CalendarActions
            eventId={editor.detailOccurrence.event_id}
            occurrenceStart={editor.detailOccurrence.occurrence_start}
            recurring={editor.detailOccurrence.recurring}
            actions={viewState.actions}
            executions={viewState.executions}
            timeZone={viewState.systemTimeZone}
            {serverUnavailable}
            onChanged={() => controller.load({ silent: true })}
            {onOpenSession}
          />
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        onClick={() => editor.openEdit(editor.detailOccurrence)}
      >
        {t('common.edit', 'Edit')}
      </Button>
      <Button
        variant="danger"
        onClick={() => editor.requestDelete(editor.detailOccurrence)}
      >
        {t('common.delete', 'Delete')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if editor.deleteTarget}
  <ConfirmDialog
    title={t('calendar.deleteTitle', 'Delete event')}
    body={editor.deleteTarget.recurring
      ? t(
          'calendar.deleteBody',
          'This event repeats. You can delete the whole series or only this occurrence.',
        )
      : t(
          'calendar.deleteBodySingle',
          'This removes the event from your calendar.',
        )}
    confirmLabel={editor.deleteTarget.recurring && editor.deleteOccurrenceOnly
      ? t('calendar.deleteOccurrence', 'Only this occurrence')
      : t('calendar.deleteSeries', 'Delete event')}
    cancelLabel={t('common.cancel', 'Cancel')}
    danger={true}
    onConfirm={editor.confirmDelete}
    onCancel={() => (editor.deleteTarget = null)}
  >
    {#snippet bodyExtra()}
      {#if editor.deleteTarget.recurring}
        <div
          class="calendar-delete-choice"
          role="radiogroup"
          aria-label={t('calendar.deleteScope', 'What to delete')}
        >
          <label>
            <input
              type="radio"
              bind:group={editor.deleteOccurrenceOnly}
              value={false}
            />
            {t('calendar.deleteSeries', 'Delete event')}
          </label>
          <label>
            <input
              type="radio"
              bind:group={editor.deleteOccurrenceOnly}
              value={true}
            />
            {t('calendar.deleteOccurrence', 'Only this occurrence')}
          </label>
        </div>
      {/if}
    {/snippet}
  </ConfirmDialog>
{/if}
