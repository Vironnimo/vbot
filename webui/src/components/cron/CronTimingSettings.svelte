<script>
  import { t } from '$lib/i18n.js';
  import {
    EVENT_TIME_UNIT_MINUTES,
    CRON_FREQUENCY_HOURLY,
    CRON_FREQUENCY_WEEKLY,
    CRON_FREQUENCY_MONTHLY,
    CRON_FREQUENCY_DAILY,
  } from '$lib/cronView.js';
  import {
    emailMailsText,
    frequencyOptions,
    intervalUnitOptions,
    weekdayOptions,
    eventTimeLabel,
    eventDirectionOptions,
    eventUnitOptions,
    eventEdgeOptions,
  } from './presentation.js';
  import { dateKeyInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  import TextField from '../ui/TextField.svelte';
  import DateField from '../ui/DateField.svelte';
  import TimeField from '../ui/TimeField.svelte';
  import Dropdown from '../Dropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  // `channels` are the Channels `channel.list` returned; an email job offers
  // the email Channels of its Agent.
  let { editor, channels = [], systemTimezone } = $props();
  let emailChannelOptions = $derived.by(() => {
    const values = editor.formValues;
    const options = channels
      .filter(
        (channel) =>
          channel.agent_id === values.agent_id && channel.platform === 'email',
      )
      .map((channel) => ({ value: channel.id, label: channel.id }));
    // A stored Channel that is not offered stays selectable, so the form shows
    // what is saved and the save names the cause.
    const stored = values.email_channel_id;
    if (stored && !options.some((option) => option.value === stored)) {
      options.push({ value: stored, label: stored });
    }
    return options;
  });
  // Choosing email preselects the Agent's only email Channel.
  function chooseFrequency(frequency) {
    editor.updateSchedule({ frequency });
    if (
      editor.isEmailSchedule &&
      !editor.formValues.email_channel_id &&
      emailChannelOptions.length === 1
    ) {
      editor.updateFormField('email_channel_id', emailChannelOptions[0].value);
    }
  }
  const scheduleFrequencies = frequencyOptions();
  const intervalUnits = intervalUnitOptions();
  const weekdays = weekdayOptions();
  const eventDirections = eventDirectionOptions();
  const eventUnits = eventUnitOptions();
  const eventEdges = eventEdgeOptions();
  // Frequencies that run at a time of day.
  const TIMED_FREQUENCIES = new Set([
    CRON_FREQUENCY_DAILY,
    CRON_FREQUENCY_WEEKLY,
    CRON_FREQUENCY_MONTHLY,
  ]);
  // A one-time run is edited as a date and a time and stored as one
  // "YYYY-MM-DDTHH:MM" wall-clock value in the server's time zone. The first
  // half chosen fills the other with 9:00 or today.
  const DEFAULT_RUN_TIME = '09:00';
  let runAtDate = $derived((editor.formValues.run_at ?? '').slice(0, 10));
  let runAtTime = $derived((editor.formValues.run_at ?? '').slice(11, 16));
  function updateRunAt({ date = runAtDate, time = runAtTime }) {
    const day = date || dateKeyInApplicationZone();
    editor.updateFormField('run_at', `${day}T${time || DEFAULT_RUN_TIME}`);
  }
  // The edited event time in words, from the form's event time fields.
  function editedEventTimeLabel(values) {
    const amount = Number(values.event_amount);
    const minutes =
      values.event_direction === 'at' || !Number.isInteger(amount)
        ? 0
        : amount * (EVENT_TIME_UNIT_MINUTES[values.event_unit] ?? 1);
    return eventTimeLabel(
      values.event_edge,
      values.event_direction === 'before' ? -minutes : minutes,
    );
  }
</script>

<section class="s-section" aria-labelledby="cron-section-timing">
  <header class="s-section__head">
    <h3 class="s-section__title" id="cron-section-timing">
      {t('cron.sections.timing')}
    </h3>
  </header>
  <p class="s-section__desc">
    {editor.isEventSchedule
      ? t('cron.sections.timingEventSubtitle')
      : t('cron.sections.timingSubtitle')}
  </p>
  <div class="s-section__body">
    <div class="s-group">
      {#if editor.isEventSchedule}
        <div class="s-row">
          <div class="s-row-info">
            <span class="s-row-label">{t('cron.form.event')}</span>
            <div class="s-row-desc">
              {t('cron.form.eventHelp')}
            </div>
          </div>
          <div class="s-row-control">
            <span class="cron-event-title" data-testid="cron-event-title"
              >{editor.formValues.event_title ||
                editor.formValues.event_id}</span
            >
          </div>
        </div>
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="cron-job-event-direction">
              {t('cron.eventTime.when')}
            </label>
            <div class="s-row-desc">
              {editedEventTimeLabel(editor.formValues)}
            </div>
          </div>
          <div class="s-row-control cron-interval-control">
            <Dropdown
              id="cron-job-event-direction"
              value={editor.formValues.event_direction}
              options={eventDirections}
              ariaLabel={t('cron.eventTime.when')}
              disabled={editor.submittingForm}
              triggerClass="cron-dropdown"
              listClass="cron-dropdown-list"
              onValueChange={(event_direction) =>
                editor.updateSchedule({ event_direction })}
            />
            <Dropdown
              id="cron-job-event-edge"
              value={editor.formValues.event_edge}
              options={eventEdges}
              ariaLabel={t('cron.eventTime.edge')}
              disabled={editor.submittingForm}
              triggerClass="cron-dropdown"
              listClass="cron-dropdown-list"
              onValueChange={(event_edge) =>
                editor.updateSchedule({ event_edge })}
            />
          </div>
        </div>
        {#if editor.formValues.event_direction !== 'at'}
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="cron-job-event-amount">
                {t('cron.eventTime.offset')}
              </label>
              <div class="s-row-desc">
                {t('cron.eventTime.offsetHelp')}
              </div>
            </div>
            <div class="s-row-control cron-interval-control">
              <TextField
                id="cron-job-event-amount"
                class="cron-interval-value"
                type="number"
                min="1"
                step="1"
                value={editor.formValues.event_amount}
                ariaLabel={t('cron.eventTime.amount')}
                disabled={editor.submittingForm}
                onInput={(event_amount) =>
                  editor.updateSchedule({ event_amount })}
              />
              <Dropdown
                id="cron-job-event-unit"
                value={editor.formValues.event_unit}
                options={eventUnits}
                ariaLabel={t('cron.eventTime.unit')}
                disabled={editor.submittingForm}
                triggerClass="cron-dropdown"
                listClass="cron-dropdown-list"
                onValueChange={(event_unit) =>
                  editor.updateSchedule({ event_unit })}
              />
            </div>
          </div>
        {/if}
      {:else}
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="cron-job-frequency">
              {t('cron.form.frequency')}
            </label>
            {#if editor.showsGeneratedExpression}
              <div class="s-row-desc cron-schedule-preview">
                <span>{editor.scheduleDescription}</span>
                <code
                  class="cron-schedule-preview__code"
                  aria-label={t('cron.form.cronExpression')}
                  >{editor.formValues.cron_expression}</code
                >
              </div>
            {/if}
          </div>
          <div class="s-row-control">
            <Dropdown
              id="cron-job-frequency"
              value={editor.formValues.frequency}
              options={scheduleFrequencies}
              ariaLabel={t('cron.form.frequency')}
              disabled={editor.isCreating && editor.submittingForm}
              triggerClass="cron-dropdown"
              listClass="cron-dropdown-list"
              onValueChange={chooseFrequency}
            />
          </div>
        </div>

        {#if editor.isEmailSchedule}
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="cron-job-email-channel">
                {t('cron.email.channel')}
                <span class="cron-required" aria-hidden="true">*</span>
              </label>
              <div class="s-row-desc">
                {emailChannelOptions.length === 0
                  ? t('cron.email.channelNone')
                  : t('cron.email.channelHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="cron-job-email-channel"
                value={editor.formValues.email_channel_id}
                options={emailChannelOptions}
                placeholder={t('cron.email.channelPlaceholder')}
                ariaLabel={t('cron.email.channel')}
                disabled={emailChannelOptions.length === 0 ||
                  (editor.isCreating && editor.submittingForm)}
                triggerClass="cron-dropdown"
                listClass="cron-dropdown-list"
                onValueChange={(next) =>
                  editor.updateFormField('email_channel_id', next)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="cron-job-email-senders">
                {t('cron.email.senders')}
              </label>
              <div class="s-row-desc">{t('cron.email.sendersHelp')}</div>
            </div>
            <div class="s-row-control">
              <TextField
                id="cron-job-email-senders"
                value={editor.formValues.email_senders}
                placeholder={t('cron.email.sendersPlaceholder')}
                disabled={editor.isCreating && editor.submittingForm}
                onInput={(next) =>
                  editor.updateFormField('email_senders', next)}
              />
            </div>
          </div>
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="cron-job-email-subject">
                {t('cron.email.subject')}
              </label>
              <div class="s-row-desc">{t('cron.email.subjectHelp')}</div>
            </div>
            <div class="s-row-control">
              <TextField
                id="cron-job-email-subject"
                value={editor.formValues.email_subject}
                placeholder={t('cron.email.subjectPlaceholder')}
                disabled={editor.isCreating && editor.submittingForm}
                onInput={(next) =>
                  editor.updateFormField('email_subject', next)}
              />
            </div>
          </div>
          {#if !editor.isCreating && editor.selectedJob?.email}
            <div class="s-row">
              <div class="s-row-info">
                <span class="s-row-label">{t('cron.email.lastMail')}</span>
              </div>
              <div class="s-row-control">
                <span data-testid="cron-email-mails"
                  >{emailMailsText(editor.selectedJob, systemTimezone)}</span
                >
              </div>
            </div>
          {/if}
        {:else if editor.isOnceSchedule}
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="cron-job-run-at">
                {t('cron.form.runAt')}
                <span class="cron-required" aria-hidden="true">*</span>
              </label>
              <div class="s-row-desc">
                {t('cron.form.timezoneNote', {
                  timezone: systemTimezone,
                })}
              </div>
            </div>
            <div class="s-row-control cron-run-at-control">
              <DateField
                id="cron-job-run-at"
                value={runAtDate}
                placeholder={t('dateField.choose')}
                required
                disabled={editor.isCreating && editor.submittingForm}
                onChange={(date) => updateRunAt({ date })}
              />
              <TimeField
                id="cron-job-run-at-time"
                ariaLabel={t('cron.form.time')}
                value={runAtTime}
                placeholder={t('cron.form.time')}
                required
                disabled={editor.isCreating && editor.submittingForm}
                onChange={(time) => updateRunAt({ time })}
              />
            </div>
          </div>
        {:else if editor.isIntervalSchedule}
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="cron-job-interval">
                {t('cron.form.interval')}
                <span class="cron-required" aria-hidden="true">*</span>
              </label>
              <div class="s-row-desc">
                {t('cron.form.intervalHelp')}
              </div>
            </div>
            <div class="s-row-control cron-interval-control">
              <TextField
                id="cron-job-interval"
                class="cron-interval-value"
                type="number"
                min="1"
                step="1"
                value={editor.formValues.interval_value}
                disabled={editor.isCreating && editor.submittingForm}
                onInput={(interval_value) =>
                  editor.updateSchedule({ interval_value })}
              />
              <Dropdown
                id="cron-job-interval-unit"
                value={editor.formValues.interval_unit}
                options={intervalUnits}
                ariaLabel={t('cron.form.intervalUnit')}
                disabled={editor.isCreating && editor.submittingForm}
                triggerClass="cron-dropdown"
                listClass="cron-dropdown-list"
                onValueChange={(interval_unit) =>
                  editor.updateSchedule({ interval_unit })}
              />
            </div>
          </div>
        {:else if editor.isCustomSchedule}
          <div class="s-row">
            <div class="s-row-info">
              <label
                class="s-row-label cron-label-with-hint"
                for="cron-job-expression"
              >
                {t('cron.form.cronExpression')}
                <InfoHint text={t('cron.form.cronExpressionHelp')} />
                <span class="cron-required" aria-hidden="true">*</span>
              </label>
              {#if editor.scheduleDescription}
                <div class="s-row-desc">
                  {editor.scheduleDescription}
                </div>
              {/if}
            </div>
            <div class="s-row-control">
              <TextField
                id="cron-job-expression"
                code
                value={editor.formValues.cron_expression}
                placeholder={t('cron.form.cronExpressionPlaceholder')}
                disabled={editor.isCreating && editor.submittingForm}
                onInput={(cron_expression) =>
                  editor.updateSchedule({ cron_expression })}
              />
            </div>
          </div>
        {:else}
          {#if editor.formValues.frequency === CRON_FREQUENCY_HOURLY}
            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="cron-job-minute">
                  {t('cron.form.minute')}
                </label>
                <div class="s-row-desc">
                  {t('cron.form.minuteHelp')}
                </div>
              </div>
              <div class="s-row-control s-row-control--number">
                <TextField
                  id="cron-job-minute"
                  type="number"
                  min="0"
                  max="59"
                  step="1"
                  value={editor.formValues.minute}
                  disabled={editor.isCreating && editor.submittingForm}
                  onInput={(minute) => editor.updateSchedule({ minute })}
                />
              </div>
            </div>
          {/if}

          {#if editor.formValues.frequency === CRON_FREQUENCY_WEEKLY}
            <div class="s-row">
              <div class="s-row-info">
                <span class="s-row-label" id="cron-weekdays-label">
                  {t('cron.form.weekdays')}
                </span>
              </div>
              <div class="s-row-control">
                <div
                  class="cron-weekdays"
                  role="group"
                  aria-labelledby="cron-weekdays-label"
                >
                  {#each weekdays as weekday (weekday.day)}
                    <button
                      type="button"
                      class="cron-weekday"
                      aria-pressed={editor.formValues.weekdays.includes(
                        weekday.day,
                      )}
                      aria-label={weekday.long}
                      data-testid={`cron-weekday-${weekday.day}`}
                      disabled={editor.isCreating && editor.submittingForm}
                      onclick={() => editor.toggleWeekday(weekday.day)}
                    >
                      {weekday.short}
                    </button>
                  {/each}
                </div>
              </div>
            </div>
          {/if}

          {#if editor.formValues.frequency === CRON_FREQUENCY_MONTHLY}
            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="cron-job-month-day">
                  {t('cron.form.monthDay')}
                </label>
                {#if Number(editor.formValues.month_day) > 28}
                  <div class="s-row-desc">
                    {t('cron.form.monthDayHelp')}
                  </div>
                {/if}
              </div>
              <div class="s-row-control s-row-control--number">
                <TextField
                  id="cron-job-month-day"
                  type="number"
                  min="1"
                  max="31"
                  step="1"
                  value={editor.formValues.month_day}
                  disabled={editor.isCreating && editor.submittingForm}
                  onInput={(month_day) => editor.updateSchedule({ month_day })}
                />
              </div>
            </div>
          {/if}

          {#if TIMED_FREQUENCIES.has(editor.formValues.frequency)}
            <div class="s-row">
              <div class="s-row-info">
                <label class="s-row-label" for="cron-job-time">
                  {t('cron.form.time')}
                </label>
                <div class="s-row-desc">
                  {t('cron.form.timezoneNote', {
                    timezone: systemTimezone,
                  })}
                </div>
              </div>
              <div class="s-row-control s-row-control--number">
                <TimeField
                  id="cron-job-time"
                  value={editor.formValues.time}
                  required
                  disabled={editor.isCreating && editor.submittingForm}
                  onChange={(time) => editor.updateSchedule({ time })}
                />
              </div>
            </div>
          {/if}
        {/if}
      {/if}
      {#if !editor.isOnceSchedule && !editor.isEventSchedule}
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <label class="s-row-label" for="cron-job-repeat">
              {t('cron.form.repeat')}
            </label>
          </div>
          <div class="s-row-control s-row-control--number">
            <TextField
              id="cron-job-repeat"
              type="number"
              min="1"
              step="1"
              value={editor.formValues.repeat}
              placeholder={t('cron.form.repeatPlaceholder')}
              disabled={editor.isCreating && editor.submittingForm}
              onInput={(next) => editor.updateFormField('repeat', next)}
            />
          </div>
        </div>
      {/if}
    </div>
  </div>
</section>
