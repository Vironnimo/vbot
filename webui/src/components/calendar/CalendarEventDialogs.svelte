<script>
  // The Calendar's event dialogs: the create and edit form, the card with one
  // occurrence's details beside the entry that opened it, and the delete
  // question, which stacks over that card.
  import { t } from '$lib/i18n.js';
  import { monthlyChoices } from '$lib/calendarEventForm.js';
  import { eventById, todayKey } from '$lib/calendarView.js';
  import { weekdayNames } from '$lib/dateTimeFields.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import Modal from '../ui/Modal.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import DateField from '../ui/DateField.svelte';
  import TimeField from '../ui/TimeField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import Dropdown from '../Dropdown.svelte';
  import CalendarEventJobs from '../CalendarEventJobs.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import CalendarEventCard from './CalendarEventCard.svelte';
  import {
    monthlyLabel,
    occurrenceWhen,
    recurrenceSummary,
  } from './presentation.js';

  let {
    editor,
    viewState,
    controller,
    serverUnavailable,
    agentsRefreshToken,
    onOpenSession,
    occurrenceJobs,
  } = $props();

  const WEEKDAY_CODES = ['mo', 'tu', 'we', 'th', 'fr', 'sa', 'su'];
  // The unit after "Every n", by the repetition's frequency.
  const INTERVAL_UNITS = {
    daily: (count) =>
      count === 1 ? t('calendar.form.unit.day') : t('calendar.form.unit.days'),
    weekly: (count) =>
      count === 1
        ? t('calendar.form.unit.week')
        : t('calendar.form.unit.weeks'),
    monthly: (count) =>
      count === 1
        ? t('calendar.form.unit.month')
        : t('calendar.form.unit.months'),
    yearly: (count) =>
      count === 1
        ? t('calendar.form.unit.year')
        : t('calendar.form.unit.years'),
  };

  let weekdays = $derived(
    weekdayNames().map((label, index) => ({
      code: WEEKDAY_CODES[index],
      label,
    })),
  );
  // A rule the form cannot show as choices stays selectable as custom text.
  let recurrenceOptions = $derived([
    { value: 'none', label: t('calendar.form.freqNone') },
    { value: 'daily', label: t('calendar.form.freqDaily') },
    { value: 'weekly', label: t('calendar.form.freqWeekly') },
    { value: 'monthly', label: t('calendar.form.freqMonthly') },
    { value: 'yearly', label: t('calendar.form.freqYearly') },
    ...(editor.formValues.custom_rrule
      ? [{ value: 'custom', label: t('calendar.form.freqCustom') }]
      : []),
  ]);
  let monthlyOptions = $derived(
    monthlyChoices(editor.formValues.start_date).map((choice) => ({
      value: choice,
      label: monthlyLabel(choice, editor.formValues.start_date),
    })),
  );
  let endModeOptions = $derived([
    { value: 'never', label: t('calendar.form.endsNever') },
    { value: 'count', label: t('calendar.form.endsCount') },
    { value: 'until', label: t('calendar.form.endsUntil') },
  ]);
  let editScopes = $derived([
    { value: 'series', label: t('calendar.form.editSeries') },
    { value: 'occurrence', label: t('calendar.form.editOccurrence') },
  ]);
  let deleteScopes = $derived([
    { value: false, label: t('calendar.deleteSeries') },
    { value: true, label: t('calendar.deleteOccurrence') },
  ]);

  function intervalUnit(values) {
    const count = Number(values.interval);
    return INTERVAL_UNITS[values.freq]?.(count) ?? '';
  }

  function seriesOf(occurrence) {
    return eventById(viewState.events, occurrence.event_id) ?? occurrence;
  }

  function deleteLabel(target) {
    if (!target.recurring) return t('calendar.deleteEvent');
    return editor.deleteOccurrenceOnly
      ? t('calendar.deleteOccurrenceAction')
      : t('calendar.deleteSeriesAction');
  }
</script>

{#if editor.formOpen}
  {@const values = editor.formValues}
  {@const repeats = editor.editScope === 'series'}
  {@const repetition = values.freq !== 'none' && values.freq !== 'custom'}
  <Modal
    title={editor.formMode === 'edit'
      ? t('calendar.form.editTitle')
      : t('calendar.form.createTitle')}
    labelledById="calendar-form-title"
    size="md"
    onClose={() => (editor.formOpen = false)}
  >
    {#snippet body()}
      <form
        class="calendar-form"
        id="calendar-event-form"
        onsubmit={(event) => {
          event.preventDefault();
          editor.submitForm();
        }}
      >
        <div class="modal-body">
          {#if editor.canEditOccurrence}
            <div
              class="calendar-segmented calendar-edit-scope"
              role="radiogroup"
              aria-label={t('calendar.form.editScope')}
            >
              {#each editScopes as scope (scope.value)}
                <label
                  class="calendar-segmented__option"
                  class:is-active={editor.editScope === scope.value}
                >
                  <input
                    type="radio"
                    name="calendar-edit-scope"
                    checked={editor.editScope === scope.value}
                    onchange={() => editor.setEditScope(scope.value)}
                  />
                  {scope.label}
                </label>
              {/each}
            </div>
          {/if}
          <FormField
            label={t('calendar.form.title')}
            controlId="calendar-form-title-input"
          >
            <TextField
              id="calendar-form-title-input"
              value={values.title}
              onInput={(next) => (values.title = next)}
              placeholder={t('calendar.form.titlePlaceholder')}
            />
          </FormField>
          <div class="calendar-form-when" class:is-all-day={values.all_day}>
            <FormField
              label={values.all_day
                ? t('calendar.form.firstDay')
                : t('calendar.form.startDate')}
              controlId="calendar-form-start-date"
            >
              <DateField
                id="calendar-form-start-date"
                value={values.start_date}
                required
                onChange={(next) => editor.setStart({ start_date: next })}
              />
            </FormField>
            {#if !values.all_day}
              <FormField
                label={t('calendar.form.startTime')}
                controlId="calendar-form-start-time"
              >
                <TimeField
                  id="calendar-form-start-time"
                  value={values.start_time}
                  required
                  onChange={(next) => editor.setStart({ start_time: next })}
                />
              </FormField>
            {/if}
            <FormField
              label={values.all_day
                ? t('calendar.form.lastDay')
                : t('calendar.form.endDate')}
              controlId="calendar-form-end-date"
            >
              <DateField
                id="calendar-form-end-date"
                value={values.end_date}
                min={values.start_date}
                required
                onChange={(next) => (values.end_date = next)}
              />
            </FormField>
            {#if !values.all_day}
              <FormField
                label={t('calendar.form.endTime')}
                controlId="calendar-form-end-time"
              >
                <TimeField
                  id="calendar-form-end-time"
                  value={values.end_time}
                  required
                  onChange={(next) => (values.end_time = next)}
                />
              </FormField>
            {/if}
          </div>
          <!-- One occurrence keeps the event's kind, timed or all-day. -->
          {#if repeats}
            <FormField
              class="calendar-form-toggle"
              label={t('calendar.form.allDay')}
              controlId="calendar-form-all-day"
            >
              <Toggle
                id="calendar-form-all-day"
                checked={values.all_day}
                onChange={(next) => editor.setAllDay(next)}
                size="sm"
              />
            </FormField>
            <div class="calendar-form-pair">
              <FormField
                label={t('calendar.form.recurrence')}
                controlId="calendar-form-freq"
              >
                <Dropdown
                  id="calendar-form-freq"
                  value={values.freq}
                  options={recurrenceOptions}
                  ariaLabel={t('calendar.form.recurrence')}
                  onValueChange={(next) => editor.setFrequency(next)}
                />
              </FormField>
              {#if repetition}
                <FormField
                  label={t('calendar.form.interval')}
                  controlId="calendar-form-interval"
                >
                  <div class="calendar-form-inline">
                    <TextField
                      id="calendar-form-interval"
                      class="calendar-form-number"
                      type="number"
                      value={values.interval}
                      onInput={(next) => (values.interval = next)}
                      min="1"
                    />
                    <span class="calendar-form-unit"
                      >{intervalUnit(values)}</span
                    >
                  </div>
                </FormField>
              {/if}
            </div>
            {#if values.freq === 'custom'}
              <FormField
                label={t('calendar.form.rrule')}
                controlId="calendar-form-rrule"
              >
                <TextField
                  id="calendar-form-rrule"
                  code
                  value={values.custom_rrule}
                  onInput={(next) => (values.custom_rrule = next)}
                />
              </FormField>
            {:else if values.freq === 'weekly'}
              <FormField
                label={t('calendar.form.weekdays')}
                controlId="calendar-form-weekdays"
              >
                <div
                  class="calendar-weekday-picker"
                  id="calendar-form-weekdays"
                >
                  {#each weekdays as weekday (weekday.code)}
                    <button
                      type="button"
                      class="calendar-weekday-option"
                      class:is-active={values.by_weekday.includes(weekday.code)}
                      aria-pressed={values.by_weekday.includes(weekday.code)}
                      onclick={() => editor.toggleWeekday(weekday.code)}
                    >
                      {weekday.label}
                    </button>
                  {/each}
                </div>
              </FormField>
            {:else if values.freq === 'monthly'}
              <FormField
                label={t('calendar.form.monthlyOn')}
                controlId="calendar-form-monthly"
              >
                <Dropdown
                  id="calendar-form-monthly"
                  value={values.monthly_by}
                  options={monthlyOptions}
                  ariaLabel={t('calendar.form.monthlyOn')}
                  onValueChange={(next) => (values.monthly_by = next)}
                />
              </FormField>
            {/if}
            {#if repetition}
              <FormField
                label={t('calendar.form.ends')}
                controlId="calendar-form-end-mode"
              >
                <div class="calendar-form-ends">
                  <Dropdown
                    id="calendar-form-end-mode"
                    value={values.end_mode}
                    options={endModeOptions}
                    ariaLabel={t('calendar.form.ends')}
                    onValueChange={(next) => (values.end_mode = next)}
                  />
                  {#if values.end_mode === 'count'}
                    <div class="calendar-form-inline">
                      <TextField
                        class="calendar-form-number"
                        type="number"
                        value={values.end_count}
                        onInput={(next) => (values.end_count = next)}
                        min="1"
                        ariaLabel={t('calendar.form.endsCount')}
                      />
                      <span class="calendar-form-unit"
                        >{t('calendar.form.times')}</span
                      >
                    </div>
                  {:else if values.end_mode === 'until'}
                    <DateField
                      value={values.end_until}
                      min={values.start_date}
                      placeholder={t('dateField.choose')}
                      onChange={(next) => (values.end_until = next)}
                      ariaLabel={t('calendar.form.endsUntil')}
                    />
                  {/if}
                </div>
              </FormField>
            {/if}
          {/if}
          <FormField
            label={t('calendar.form.location')}
            controlId="calendar-form-location"
          >
            <TextField
              id="calendar-form-location"
              value={values.location}
              onInput={(next) => (values.location = next)}
              placeholder={t('calendar.form.optional')}
            />
          </FormField>
          <FormField
            label={t('calendar.form.description')}
            controlId="calendar-form-description"
          >
            <TextArea
              id="calendar-form-description"
              value={values.description}
              onInput={(next) => (values.description = next)}
              rows={3}
              placeholder={t('calendar.form.optional')}
            />
          </FormField>
          {#if editor.formError}
            <Banner variant="error">{editor.formError}</Banner>
          {/if}
        </div>
        <div class="modal-footer">
          <Button variant="secondary" onClick={() => (editor.formOpen = false)}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" type="submit" loading={editor.submitting}>
            {editor.formMode === 'edit'
              ? t('common.save')
              : t('calendar.form.create')}
          </Button>
        </div>
      </form>
    {/snippet}
  </Modal>
{/if}

{#if editor.detailOpen && editor.detailOccurrence}
  {@const occurrence = editor.detailOccurrence}
  <CalendarEventCard
    title={occurrence.title}
    occurrenceId={occurrence.id}
    anchor={editor.detailAnchor}
    onClose={editor.closeDetail}
    onEdit={() => editor.openEdit(occurrence)}
    onDelete={() => editor.requestDelete(occurrence)}
  >
    {#snippet details()}
      <div class="calendar-detail-fact">
        <svg viewBox="0 0 16 16" aria-hidden="true"
          ><circle cx="8" cy="8" r="5.75" /><path d="M8 4.75V8l2.25 1.5" /></svg
        >
        <div>
          <p class="calendar-detail-time">
            {occurrenceWhen(occurrence, {
              timeZone: viewState.systemTimeZone,
              currentDayKey: todayKey(viewState.systemTimeZone),
            })}
          </p>
          {#if occurrence.recurring}
            <p class="calendar-detail-meta">
              {recurrenceSummary(seriesOf(occurrence))}{occurrence.overridden
                ? ` · ${t('calendar.detail.changedOccurrence')}`
                : ''}
            </p>
          {/if}
        </div>
      </div>
      {#if occurrence.location}
        <div class="calendar-detail-fact">
          <svg viewBox="0 0 16 16" aria-hidden="true"
            ><path
              d="M8 14.25s4.25-3.9 4.25-7.5a4.25 4.25 0 1 0-8.5 0c0 3.6 4.25 7.5 4.25 7.5Z"
            /><circle cx="8" cy="6.75" r="1.5" /></svg
          >
          <p class="calendar-detail-line">{occurrence.location}</p>
        </div>
      {/if}
      {#if occurrence.description}
        <div class="calendar-detail-fact">
          <svg viewBox="0 0 16 16" aria-hidden="true"
            ><path d="M2.75 4h10.5M2.75 8h10.5M2.75 12h6.5" /></svg
          >
          <p class="calendar-detail-notes">{occurrence.description}</p>
        </div>
      {/if}
      <CalendarEventJobs
        eventId={occurrence.event_id}
        {occurrence}
        jobs={viewState.jobs}
        jobsError={viewState.jobsError}
        timeZone={viewState.systemTimeZone}
        {serverUnavailable}
        {agentsRefreshToken}
        onChanged={() => controller.load({ silent: true })}
        {onOpenSession}
      />
    {/snippet}
  </CalendarEventCard>
{/if}

{#if editor.deleteTarget}
  {@const target = editor.deleteTarget}
  {@const jobCount = occurrenceJobs(target).length}
  <ConfirmDialog
    title={t('calendar.deleteTitle', { title: target.title })}
    body={target.recurring
      ? t('calendar.deleteBody')
      : t('calendar.deleteBodySingle')}
    confirmLabel={deleteLabel(target)}
    cancelLabel={t('common.cancel')}
    danger={true}
    onConfirm={editor.confirmDelete}
    onCancel={() => (editor.deleteTarget = null)}
  >
    {#snippet bodyExtra()}
      {#if target.recurring}
        <div
          class="calendar-segmented calendar-delete-choice"
          role="radiogroup"
          aria-label={t('calendar.deleteScope')}
        >
          {#each deleteScopes as scope (scope.value)}
            <label
              class="calendar-segmented__option"
              class:is-active={editor.deleteOccurrenceOnly === scope.value}
            >
              <input
                type="radio"
                name="calendar-delete-scope"
                bind:group={editor.deleteOccurrenceOnly}
                value={scope.value}
              />
              {scope.label}
            </label>
          {/each}
        </div>
      {/if}
      {#if jobCount > 0 && !(target.recurring && editor.deleteOccurrenceOnly)}
        <p class="calendar-delete-jobs">
          {jobCount === 1
            ? t('calendar.deleteJob')
            : t('calendar.deleteJobs', { count: jobCount })}
        </p>
      {/if}
    {/snippet}
  </ConfirmDialog>
{/if}
