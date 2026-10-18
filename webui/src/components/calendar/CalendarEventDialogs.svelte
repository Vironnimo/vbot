<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import { monthlyChoices, weekOfMonth } from '$lib/calendarEventForm.js';
  import { dayKeyToUtcDate } from '$lib/calendarView.js';
  import Button from '../ui/Button.svelte';
  import Banner from '../ui/Banner.svelte';
  import Modal from '../ui/Modal.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import Dropdown from '../Dropdown.svelte';
  import CalendarEventJobs from '../CalendarEventJobs.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  let {
    editor,
    viewState,
    controller,
    serverUnavailable,
    agentsRefreshToken,
    onOpenSession,
    occurrenceHeading,
    recurrenceText,
    occurrenceJobs,
  } = $props();
  let locale = $derived(activeLocaleTag());
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
  const ORDINALS = {
    1: () => t('calendar.form.nthFirst'),
    2: () => t('calendar.form.nthSecond'),
    3: () => t('calendar.form.nthThird'),
    4: () => t('calendar.form.nthFourth'),
  };

  // A monthly repetition of a start day: "Day 15", "The third Tuesday", "The
  // last Tuesday".
  function monthlyLabel(choice, dayKey) {
    if (choice === 'day') {
      return t('calendar.form.monthlyDay', {
        day: Number(String(dayKey).slice(8, 10)),
      });
    }
    const weekday = new Intl.DateTimeFormat(locale, {
      weekday: 'long',
      timeZone: 'UTC',
    }).format(dayKeyToUtcDate(dayKey));
    const nth =
      choice === 'last'
        ? t('calendar.form.nthLast')
        : (ORDINALS[weekOfMonth(dayKey)]?.() ?? '');
    return t('calendar.form.monthlyWeekday', { nth, weekday });
  }
</script>

{#if editor.formOpen}
  {@const values = editor.formValues}
  {@const repeats = editor.editScope === 'series'}
  <Modal
    title={editor.formMode === 'edit'
      ? t('calendar.form.editTitle')
      : t('calendar.form.createTitle')}
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
          {#if editor.canEditOccurrence}
            <div
              class="calendar-edit-scope"
              role="radiogroup"
              aria-label={t('calendar.form.editScope')}
            >
              <label>
                <input
                  type="radio"
                  name="calendar-edit-scope"
                  checked={editor.editScope === 'series'}
                  onchange={() => editor.setEditScope('series')}
                />
                {t('calendar.form.editSeries')}
              </label>
              <label>
                <input
                  type="radio"
                  name="calendar-edit-scope"
                  checked={editor.editScope === 'occurrence'}
                  onchange={() => editor.setEditScope('occurrence')}
                />
                {t('calendar.form.editOccurrence')}
              </label>
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
          <div class="calendar-form-row">
            <FormField
              label={values.all_day
                ? t('calendar.form.firstDay')
                : t('calendar.form.startDate')}
              controlId="calendar-form-start-date"
            >
              <TextField
                id="calendar-form-start-date"
                type="date"
                value={values.start_date}
                onInput={(next) => editor.setStart({ start_date: next })}
              />
            </FormField>
            {#if !values.all_day}
              <FormField
                label={t('calendar.form.startTime')}
                controlId="calendar-form-start-time"
              >
                <TextField
                  id="calendar-form-start-time"
                  type="time"
                  value={values.start_time}
                  onInput={(next) => editor.setStart({ start_time: next })}
                />
              </FormField>
            {/if}
            <FormField
              label={values.all_day
                ? t('calendar.form.lastDay')
                : t('calendar.form.endDate')}
              controlId="calendar-form-end-date"
            >
              <TextField
                id="calendar-form-end-date"
                type="date"
                value={values.end_date}
                onInput={(next) => (values.end_date = next)}
              />
            </FormField>
            {#if !values.all_day}
              <FormField
                label={t('calendar.form.endTime')}
                controlId="calendar-form-end-time"
              >
                <TextField
                  id="calendar-form-end-time"
                  type="time"
                  value={values.end_time}
                  onInput={(next) => (values.end_time = next)}
                />
              </FormField>
            {/if}
          </div>
          <!-- One occurrence keeps the event's kind, timed or all-day. -->
          {#if repeats}
            <div class="calendar-form-toggle">
              <span>{t('calendar.form.allDay')}</span>
              <Toggle
                checked={values.all_day}
                onChange={(next) => editor.setAllDay(next)}
                size="sm"
                ariaLabel={t('calendar.form.allDay')}
              />
            </div>
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
            {:else if values.freq !== 'none'}
              <FormField
                label={t('calendar.form.interval')}
                controlId="calendar-form-interval"
              >
                <TextField
                  id="calendar-form-interval"
                  type="number"
                  value={values.interval}
                  onInput={(next) => (values.interval = next)}
                  min="1"
                  ariaLabel={t('calendar.form.interval')}
                />
              </FormField>
              {#if values.freq === 'weekly'}
                <FormField
                  label={t('calendar.form.weekdays')}
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
                        class:is-active={values.by_weekday.includes(code)}
                        aria-pressed={values.by_weekday.includes(code)}
                        onclick={() => editor.toggleWeekday(code)}
                      >
                        {label}
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
                    <TextField
                      type="number"
                      value={values.end_count}
                      onInput={(next) => (values.end_count = next)}
                      min="1"
                      ariaLabel={t('calendar.form.endsCount')}
                    />
                    <span class="calendar-form-ends-unit"
                      >{t('calendar.form.times')}</span
                    >
                  {:else if values.end_mode === 'until'}
                    <TextField
                      type="date"
                      value={values.end_until}
                      onInput={(next) => (values.end_until = next)}
                      ariaLabel={t('calendar.form.endsUntil')}
                    />
                  {/if}
                </div>
              </FormField>
            {/if}
          {/if}
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
        </form>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={() => (editor.formOpen = false)}>
        {t('common.cancel')}
      </Button>
      <Button
        variant="primary"
        onClick={editor.submitForm}
        disabled={editor.submitting}
      >
        {editor.formMode === 'edit'
          ? t('common.save')
          : t('calendar.form.create')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if editor.detailOpen && editor.detailOccurrence}
  {@const occurrence = editor.detailOccurrence}
  <Modal
    title={occurrence.title}
    labelledById="calendar-detail-title"
    onClose={editor.closeDetail}
  >
    {#snippet body()}
      <div class="modal-body">
        <div class="calendar-detail">
          <p class="calendar-detail-time">
            {occurrenceHeading(occurrence)}
          </p>
          {#if occurrence.location}
            <p class="calendar-detail-line">
              <span class="calendar-detail-label"
                >{t('calendar.form.location')}</span
              >
              {occurrence.location}
            </p>
          {/if}
          {#if occurrence.description}
            <p class="calendar-detail-notes">{occurrence.description}</p>
          {/if}
          {#if occurrence.recurring}
            <p class="calendar-detail-meta">
              {recurrenceText(occurrence)}{occurrence.overridden
                ? ` · ${t('calendar.detail.changedOccurrence')}`
                : ''}
            </p>
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
        </div>
      </div>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={() => editor.openEdit(occurrence)}>
        {t('common.edit')}
      </Button>
      <Button variant="danger" onClick={() => editor.requestDelete(occurrence)}>
        {t('common.delete')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if editor.deleteTarget}
  {@const target = editor.deleteTarget}
  {@const jobCount = occurrenceJobs(target).length}
  <ConfirmDialog
    title={t('calendar.deleteTitle')}
    body={target.recurring
      ? t('calendar.deleteBody')
      : t('calendar.deleteBodySingle')}
    confirmLabel={target.recurring && editor.deleteOccurrenceOnly
      ? t('calendar.deleteOccurrence')
      : t('calendar.deleteSeries')}
    cancelLabel={t('common.cancel')}
    danger={true}
    onConfirm={editor.confirmDelete}
    onCancel={() => (editor.deleteTarget = null)}
  >
    {#snippet bodyExtra()}
      {#if target.recurring}
        <div
          class="calendar-delete-choice"
          role="radiogroup"
          aria-label={t('calendar.deleteScope')}
        >
          <label>
            <input
              type="radio"
              bind:group={editor.deleteOccurrenceOnly}
              value={false}
            />
            {t('calendar.deleteSeries')}
          </label>
          <label>
            <input
              type="radio"
              bind:group={editor.deleteOccurrenceOnly}
              value={true}
            />
            {t('calendar.deleteOccurrence')}
          </label>
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
