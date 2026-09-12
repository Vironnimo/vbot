<script>
  import {
    displayValue,
    listNextRun,
    scheduleKindLabel,
    scheduleSummary,
    scheduleTechnicalValue,
    sessionSummary,
    lastResultSupport,
    remainingRunsLabel,
    statusLabel,
    statusChipVariant,
    outcomeLabel,
    isTerminalJob,
  } from './cron/presentation.js';

  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import Banner from './ui/Banner.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import StatusChip from './ui/StatusChip.svelte';
  import Toggle from './ui/Toggle.svelte';
  import {
    CRON_STATUS_ACTIVE,
    CRON_SCHEDULE_TYPE_INTERVAL,
    CRON_SCHEDULE_TYPE_ONCE,
    CRON_SCHEDULE_TYPE_CRON,
    applyAgentListResponse,
    applyCronListResponse,
    buildCronAgentDropdownOptions,
    buildCronAgentOptions,
    createCronViewState,
    visibleCronJobs,
  } from '$lib/cronView.js';
  import FormField from './ui/FormField.svelte';
  import TextField from './ui/TextField.svelte';
  import Dropdown from './Dropdown.svelte';
  import TextArea from './ui/TextArea.svelte';
  import InfoHint from './ui/InfoHint.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onMount } from 'svelte';
  import {
    listAgents,
    listCronJobs,
    listProjects,
    showProject,
  } from '$lib/api.js';
  import { createAgentTargetCatalogLoader } from '$lib/agentTargetOptions.js';
  import { createCronEditor } from './cron/editor.svelte.js';

  const noop = () => {};

  let {
    onToast = noop,
    serverUnavailable = false,
    cronRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
    targetJobId = '',
  } = $props();
  const editor = createCronEditor({
    get jobs() {
      return jobs;
    },
    get viewState() {
      return viewState;
    },
    get loadProjectTeams() {
      return loadProjectTeams;
    },
    get destroyed() {
      return destroyed;
    },
    get loadJobs() {
      return loadJobs;
    },
    get showToast() {
      return showToast;
    },
    get errorMessageText() {
      return errorMessageText;
    },
  });

  let viewState = $state(createCronViewState());

  // Project teams power the project-agent options in the cron dropdown. They are
  // loaded lazily the first time the detail pane renders a form and cached for
  // the lifetime of the view, so the N+1 `project.show` scan (one per project)
  // never runs on every render — only once, on demand.
  let projectTeams = $state([]);
  let projectTeamsLoaded = false;
  const targetCatalog = createAgentTargetCatalogLoader({
    listProjects,
    showProject,
  });

  let destroyed = false;
  let jobsRequestId = 0;
  let agentsRequestId = 0;
  let lastCronRefreshToken = 0;
  let lastAgentsRefreshToken = 0;
  let lastProjectsRefreshToken = 0;

  let hasAgents = $derived(viewState.agents.length > 0);
  let isLoading = $derived(viewState.loadingAgents || viewState.loadingJobs);
  let jobs = $derived(
    visibleCronJobs(viewState.jobs, viewState.systemTimezone),
  );

  // Identity agents (bare ids, unchanged) plus project agents addressed as
  // `agent@projekt`. A project option's value IS the address, so saving sends it
  // straight through as the `agent_id` param. Group headers are inserted only
  // when project agents exist, so the identity-only dropdown is unchanged.
  let agentOptions = $derived(
    buildCronAgentDropdownOptions(viewState.agents, projectTeams, {
      identityGroupLabel: t('cron.form.agentGroup.identity', 'Identity agents'),
      projectGroupLabel: t('cron.form.agentGroup.project', 'Project agents'),
    }),
  );
  // Map every selectable option value (bare id or address) to its label so the
  // job list can render a readable target. Built from the header-free options so
  // the group separators never enter the map; an identity agent maps to its name
  // as before, a project agent to its `agent@projekt` address.
  let agentLabelByValue = $derived(
    new Map(
      buildCronAgentOptions(viewState.agents, projectTeams).map((option) => [
        option.value,
        option.label,
      ]),
    ),
  );
  onMount(() => {
    loadInitialData();

    return () => {
      destroyed = true;
      targetCatalog.dispose();
    };
  });

  // Auto-select the first job once the list loads, unless the user is mid-create
  // or already has a selection that still exists. A targetJobId (calendar deep
  // link) wins once; it is consumed so the user can freely change selection.
  let appliedTargetJobId = $state('');
  $effect(() => {
    if (editor.isCreating || jobs.length === 0) {
      return;
    }
    if (
      targetJobId &&
      targetJobId !== appliedTargetJobId &&
      jobs.some((job) => job.id === targetJobId)
    ) {
      appliedTargetJobId = targetJobId;
      editor.selectJobNow(jobs.find((job) => job.id === targetJobId));
      return;
    }
    if (
      !jobs.some((job) => job.id === editor.selectedJobId) &&
      !editor.isDirty
    ) {
      editor.selectJobNow(jobs[0]);
    }
  });

  $effect(() => {
    const token = cronRefreshToken;
    if (token === lastCronRefreshToken) {
      return;
    }
    lastCronRefreshToken = token;
    loadJobs({ silent: true, external: true });
  });

  $effect(() => {
    const token = agentsRefreshToken;
    if (token === lastAgentsRefreshToken) {
      return;
    }
    lastAgentsRefreshToken = token;
    loadAgents();
  });

  $effect(() => {
    const token = projectsRefreshToken;
    if (token === lastProjectsRefreshToken) {
      return;
    }
    lastProjectsRefreshToken = token;
    projectTeamsLoaded = false;
    loadProjectTeams();
  });

  async function loadInitialData() {
    await Promise.all([loadAgents(), loadJobs()]);
  }

  async function loadAgents() {
    const requestId = agentsRequestId + 1;
    agentsRequestId = requestId;
    viewState.loadingAgents = true;
    viewState.agentsError = '';

    try {
      const result = await listAgents();
      if (destroyed || requestId !== agentsRequestId) {
        return;
      }

      applyAgentListResponse(viewState, result);
    } catch (error) {
      if (destroyed || requestId !== agentsRequestId) {
        return;
      }

      viewState.agentsError = `${t('cron.errors.loadAgents', 'Agents could not be loaded for cron jobs.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      if (!destroyed && requestId === agentsRequestId) {
        viewState.loadingAgents = false;
      }
    }
  }

  async function loadJobs(options = {}) {
    const requestId = jobsRequestId + 1;
    jobsRequestId = requestId;

    if (options.silent !== true) {
      viewState.loadingJobs = true;
    }
    viewState.jobsError = '';

    try {
      const result = await listCronJobs();
      if (destroyed || requestId !== jobsRequestId) {
        return;
      }

      if (options.external === true && editor.isDirty) {
        editor.pendingJobsResult = result;
        return;
      }
      editor.pendingJobsResult = null;
      applyCronListResponse(viewState, result);
    } catch (error) {
      if (destroyed || requestId !== jobsRequestId) {
        return;
      }

      viewState.jobsError = `${t('cron.errors.loadJobs', 'Cron jobs could not be loaded.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      if (!destroyed && requestId === jobsRequestId) {
        viewState.loadingJobs = false;
      }
    }
  }

  // Lazily scan project teams the first time the cron detail form renders (and
  // cache the result), so the dropdown can offer project agents as
  // `agent@projekt` without an N+1 `project.show` per render. A failure is
  // non-fatal: the dropdown still shows identity agents, and the team scan can
  // be retried on the next render.
  async function loadProjectTeams() {
    if (projectTeamsLoaded) return;
    const catalog = await targetCatalog.load();
    if (!catalog) return;
    projectTeams = catalog.projectTeams;
    projectTeamsLoaded = !catalog.projectError;
  }

  // `target` is the readable cron target: a bare agent name for an identity job,
  // the `agent@projekt` address for a project job (normalizeCronJob put the full
  // address on `job.agent_id`). When the agent is in the loaded options we show
  // its friendly label; otherwise the address itself is already readable.
  function agentLabel(target) {
    return (
      agentLabelByValue.get(target) || target || t('common.unknown', 'Unknown')
    );
  }

  // Route a message to the app-level toast stack. Error toasts are sticky by
  // default at the app level; when an error object is passed its message is
  // appended so transport failures stay diagnosable.
  function showToast(title, variant = 'success', error = null) {
    const message =
      variant === 'error' && error
        ? errorMessageText(error, t('common.unknown', 'Unknown'))
        : '';
    onToast({ title, message, variant });
  }

  function errorMessageText(error, fallback) {
    if (typeof error?.message === 'string' && error.message.trim()) {
      return error.message.trim();
    }

    if (typeof error === 'string' && error.trim()) {
      return error.trim();
    }

    return fallback;
  }
</script>

<section class="cron-view view active" aria-labelledby="cron-list-title">
  <div class="cron-layout">
    <aside
      class="cron-list-pane secondary-pane"
      aria-labelledby="cron-list-title"
    >
      <div class="pane-header secondary-pane__header">
        <span id="cron-list-title" class="secondary-pane__title">
          {t('cron.title', 'Scheduled Runs')}
        </span>
        <div class="pane-header-actions">
          <Button
            variant="primary"
            disabled={!hasAgents}
            onClick={editor.startCreate}
          >
            <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
              <path d="M7 1v12M1 7h12" />
            </svg>
            {t('common.add', 'Add')}
          </Button>
        </div>
      </div>

      <div class="cron-list-scroll secondary-pane__scroll secondary-list">
        {#if viewState.agentsError && !serverUnavailable}
          <div class="cron-load-error">
            <Banner variant="error" role="alert">
              {viewState.agentsError}
            </Banner>
            <Button variant="secondary" onClick={loadAgents}>
              {t('common.retry', 'Retry')}
            </Button>
          </div>
        {:else if !serverUnavailable && !hasAgents && !viewState.loadingAgents}
          <p class="cron-list-state cron-list-state--warn" role="status">
            {t('cron.noAgents', 'Create an agent before adding cron jobs.')}
          </p>
        {/if}

        {#if isLoading && !serverUnavailable}
          <p class="cron-list-state" role="status">
            {t('cron.loading', 'Loading cron jobs…')}
          </p>
        {:else if viewState.jobsError && !serverUnavailable}
          <div class="cron-load-error">
            <Banner variant="error" role="alert">
              {viewState.jobsError}
            </Banner>
            <Button variant="secondary" onClick={() => loadJobs()}>
              {t('common.retry', 'Retry')}
            </Button>
          </div>
        {:else if jobs.length === 0}
          <EmptyState
            title={t('cron.emptyTitle', 'No scheduled runs yet')}
            description={t(
              'cron.emptyListSubtitle',
              'Use Add to create a recurring or one-time Run.',
            )}
          />
        {:else}
          <ul
            class="cron-list"
            aria-label={t('cron.list.ariaLabel', 'Scheduled Runs')}
          >
            {#each jobs as job (job.id)}
              <li>
                <button
                  type="button"
                  class="cron-item secondary-list__item"
                  class:active={!editor.isCreating &&
                    job.id === editor.selectedJobId}
                  data-testid={`cron-item-${job.id}`}
                  onclick={() => editor.selectJob(job)}
                >
                  <span class="cron-item-inner">
                    <span class="cron-item-head">
                      <span class="cron-item-name" use:tooltip={job.name}>
                        {job.name}
                      </span>
                      <StatusChip variant={statusChipVariant(job)}>
                        {statusLabel(job.status)}
                      </StatusChip>
                    </span>
                    <span class="cron-item-next" use:tooltip={listNextRun(job)}>
                      {listNextRun(job)}
                    </span>
                  </span>
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </aside>

    {#if !editor.showDetailForm}
      <div class="cron-detail-pane">
        <EmptyState
          fill
          class="master-detail-empty"
          title={!serverUnavailable && !hasAgents
            ? t('cron.noAgents', 'Create an agent before adding cron jobs.')
            : t(
                'cron.emptySubtitle',
                'Create a recurring or one-time Run. Every fire gets a fresh Session unless you choose an existing one.',
              )}
        />
      </div>
    {:else}
      {#key editor.isCreating ? 'cron-create' : editor.selectedJobId}
        <div class="cron-detail-pane">
          <form class="cron-detail-scroll" onsubmit={editor.submitForm}>
            <div class="detail-top">
              <div>
                <div class="detail-eyebrow">
                  {editor.isCreating
                    ? t('cron.detail.kind.new', 'New schedule')
                    : scheduleKindLabel(editor.selectedJob)}
                </div>
                <div class="detail-heading">{editor.detailTitle}</div>
                <div class="detail-sub">
                  {editor.isCreating
                    ? t(
                        'cron.detail.createSubtitle',
                        'Define the task, timing, and Session for a new scheduled Run.',
                      )
                    : t('cron.detail.subtitle', '{schedule} with {agent}.', {
                        schedule: scheduleKindLabel(editor.selectedJob),
                        agent: agentLabel(editor.selectedJob?.agent_id),
                      })}
                </div>
              </div>

              <div class="detail-btns">
                {#if !editor.isCreating && editor.selectedJob}
                  <StatusChip variant={statusChipVariant(editor.selectedJob)}>
                    {statusLabel(editor.selectedJob.status)}
                  </StatusChip>
                  {#if !isTerminalJob(editor.selectedJob)}
                    <label class="cron-enabled-control">
                      <span>{t('cron.actions.enabled', 'Enabled')}</span>
                      <Toggle
                        checked={editor.selectedJob.status ===
                          CRON_STATUS_ACTIVE}
                        ariaLabel={editor.selectedJob.status ===
                        CRON_STATUS_ACTIVE
                          ? t('cron.actions.disableJob', 'Disable job {id}', {
                              id: editor.selectedJob.id,
                            })
                          : t('cron.actions.enableJob', 'Enable job {id}', {
                              id: editor.selectedJob.id,
                            })}
                        disabled={editor.submittingForm ||
                          editor.mutatingJobId === editor.selectedJob.id}
                        data-testid={`cron-toggle-${editor.selectedJob.id}`}
                        onChange={() => editor.toggleJob(editor.selectedJob)}
                      />
                    </label>
                  {/if}
                {/if}
              </div>
            </div>

            {#if !editor.isCreating && editor.selectedJob}
              <div
                class="cron-summary"
                aria-label={t('cron.detail.summary', 'Schedule summary')}
              >
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.nextFire', 'Next Run')}
                  </span>
                  <strong class="cron-summary-value">
                    {displayValue(editor.selectedJob.next_fire_at_display)}
                  </strong>
                  <span class="cron-summary-support">
                    {viewState.systemTimezone}
                  </span>
                </div>
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.cadence', 'Cadence')}
                  </span>
                  <strong class="cron-summary-value cron-summary-value--wrap">
                    {scheduleSummary(editor.selectedJob)}
                  </strong>
                  <span class="cron-summary-support cron-summary-support--mono">
                    {scheduleTechnicalValue(editor.selectedJob)}
                  </span>
                </div>
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.lastResult', 'Last result')}
                  </span>
                  <strong class="cron-summary-value">
                    {outcomeLabel(editor.selectedJob.last_outcome)}
                  </strong>
                  <span class="cron-summary-support">
                    {lastResultSupport(editor.selectedJob)}
                  </span>
                </div>
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.target', 'Target')}
                  </span>
                  <strong class="cron-summary-value">
                    {agentLabel(editor.selectedJob.agent_id)}
                  </strong>
                  <span class="cron-summary-support cron-summary-support--mono">
                    {sessionSummary(editor.selectedJob)}
                  </span>
                </div>
              </div>

              {#if editor.selectedJob.last_error}
                <Banner variant="error" role="status">
                  {editor.selectedJob.last_error}
                </Banner>
              {/if}

              <details class="cron-execution-details">
                <summary>
                  {t('cron.detail.executionDetails', 'Execution details')}
                </summary>
                <dl class="cron-execution-grid">
                  <div>
                    <dt>{t('cron.detail.lastAttempt', 'Last attempt')}</dt>
                    <dd>
                      {displayValue(editor.selectedJob.last_attempt_at_display)}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.lastFired', 'Last fired')}</dt>
                    <dd>
                      {displayValue(editor.selectedJob.last_fired_at_display)}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.lastCompleted', 'Last completed')}</dt>
                    <dd>
                      {displayValue(
                        editor.selectedJob.last_completed_at_display,
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.lastRun', 'Last Run')}</dt>
                    <dd>{displayValue(editor.selectedJob.last_run_id)}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.failures', 'Failures')}</dt>
                    <dd>{editor.selectedJob.consecutive_failures}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.remainingRuns', 'Remaining Runs')}</dt>
                    <dd>{remainingRunsLabel(editor.selectedJob)}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.scheduleId', 'Schedule ID')}</dt>
                    <dd>{editor.selectedJob.id}</dd>
                  </div>
                </dl>
              </details>
            {/if}

            <div class="cron-editor-grid">
              <section class="cron-card cron-task-card">
                <header class="cron-card-header">
                  <div>
                    <h2>{t('cron.sections.task', 'Task')}</h2>
                    <p>
                      {t(
                        'cron.sections.taskSubtitle',
                        'What the Agent should do when this schedule fires.',
                      )}
                    </p>
                  </div>
                </header>
                <div class="cron-card-body">
                  <FormField
                    controlId="cron-job-name"
                    label={t('cron.form.name', 'Name')}
                  >
                    <TextField
                      id="cron-job-name"
                      value={editor.formValues.name}
                      placeholder={t(
                        'cron.form.namePlaceholder',
                        'Optional — derived from the prompt',
                      )}
                      disabled={editor.isCreating && editor.submittingForm}
                      onInput={(value) => editor.updateFormField('name', value)}
                    />
                  </FormField>

                  <FormField
                    controlId="cron-form-agent"
                    label={t('cron.form.agent', 'Agent')}
                    required
                  >
                    <Dropdown
                      id="cron-form-agent"
                      value={editor.formValues.agent_id}
                      options={agentOptions}
                      placeholder={t(
                        'cron.form.agentPlaceholder',
                        'Select an agent',
                      )}
                      ariaLabel={t('cron.form.agent', 'Agent')}
                      disabled={!hasAgents || editor.submittingForm}
                      triggerClass="cron-dropdown"
                      listClass="cron-dropdown-list"
                      onValueChange={(value) =>
                        editor.updateFormField('agent_id', value)}
                    />
                  </FormField>

                  <FormField
                    controlId="cron-job-prompt"
                    label={t('cron.form.prompt', 'Prompt')}
                    required
                  >
                    <TextArea
                      id="cron-job-prompt"
                      class="cron-prompt-editor"
                      value={editor.formValues.prompt}
                      rows={8}
                      placeholder={t(
                        'cron.form.promptPlaceholder',
                        'Describe the run to schedule…',
                      )}
                      disabled={editor.isCreating && editor.submittingForm}
                      onInput={(value) =>
                        editor.updateFormField('prompt', value)}
                    />
                  </FormField>
                </div>
              </section>

              <div class="cron-editor-side">
                <section class="cron-card">
                  <header class="cron-card-header">
                    <div>
                      <h2>{t('cron.sections.timing', 'Timing')}</h2>
                      <p>
                        {t(
                          'cron.sections.timingSubtitle',
                          'Choose a readable preset or enter an exact schedule.',
                        )}
                      </p>
                    </div>
                  </header>
                  <div class="cron-card-body">
                    <fieldset class="cron-schedule-type">
                      <legend class="cron-schedule-type-label">
                        {t('cron.form.scheduleType', 'Schedule type')}
                      </legend>
                      <div class="cron-radio-group">
                        <label class="cron-radio-option">
                          <input
                            type="radio"
                            name="cron-schedule-type"
                            value={CRON_SCHEDULE_TYPE_CRON}
                            checked={editor.isCronSchedule}
                            disabled={editor.isCreating &&
                              editor.submittingForm}
                            onchange={() =>
                              editor.setScheduleType(CRON_SCHEDULE_TYPE_CRON)}
                          />
                          <span
                            >{t(
                              'cron.form.scheduleType.cron',
                              'Recurring',
                            )}</span
                          >
                        </label>
                        <label class="cron-radio-option">
                          <input
                            type="radio"
                            name="cron-schedule-type"
                            value={CRON_SCHEDULE_TYPE_INTERVAL}
                            checked={editor.isIntervalSchedule}
                            disabled={editor.isCreating &&
                              editor.submittingForm}
                            onchange={() =>
                              editor.setScheduleType(
                                CRON_SCHEDULE_TYPE_INTERVAL,
                              )}
                          />
                          <span
                            >{t(
                              'cron.form.scheduleType.interval',
                              'Interval',
                            )}</span
                          >
                        </label>
                        <label class="cron-radio-option">
                          <input
                            type="radio"
                            name="cron-schedule-type"
                            value={CRON_SCHEDULE_TYPE_ONCE}
                            checked={editor.isOnceSchedule}
                            disabled={editor.isCreating &&
                              editor.submittingForm}
                            onchange={() =>
                              editor.setScheduleType(CRON_SCHEDULE_TYPE_ONCE)}
                          />
                          <span>{t('cron.form.scheduleType.once', 'Once')}</span
                          >
                        </label>
                      </div>
                    </fieldset>

                    {#if editor.isCronSchedule}
                      <FormField
                        controlId="cron-job-preset"
                        label={t('cron.form.preset', 'Schedule preset')}
                      >
                        <Dropdown
                          id="cron-job-preset"
                          value={editor.selectedPreset}
                          options={editor.presetOptions}
                          ariaLabel={t('cron.form.preset', 'Schedule preset')}
                          disabled={editor.isCreating && editor.submittingForm}
                          triggerClass="cron-dropdown"
                          listClass="cron-dropdown-list"
                          onValueChange={editor.applyPreset}
                        />
                      </FormField>

                      <FormField controlId="cron-job-expression" required>
                        {#snippet labelContent()}
                          {t('cron.form.cronExpression', 'Cron expression')}
                          <InfoHint
                            text={t(
                              'cron.form.cronExpressionHelp',
                              'Five space-separated fields: minute, hour, day of month, month, weekday.\n\nExample: 0 9 * * 1-5 runs at 09:00 on weekdays. * matches any value; ranges (1-5) and lists (1,3,5) work in every field.',
                            )}
                          />
                        {/snippet}
                        <TextField
                          id="cron-job-expression"
                          value={editor.formValues.cron_expression}
                          placeholder={t(
                            'cron.form.cronExpressionPlaceholder',
                            '0 9 * * 1-5',
                          )}
                          disabled={editor.isCreating && editor.submittingForm}
                          onInput={(next) => editor.updateCronExpression(next)}
                        />
                        {#if editor.cronExpressionPreview}
                          <span class="cron-expression-preview">
                            {editor.cronExpressionPreview}
                          </span>
                        {/if}
                      </FormField>
                    {:else if editor.isIntervalSchedule}
                      <FormField
                        controlId="cron-job-interval"
                        label={t(
                          'cron.form.intervalMinutes',
                          'Every (minutes)',
                        )}
                        required
                      >
                        <TextField
                          id="cron-job-interval"
                          type="number"
                          min="1"
                          step="1"
                          value={editor.formValues.interval_minutes}
                          placeholder={t(
                            'cron.form.intervalMinutesPlaceholder',
                            '120',
                          )}
                          disabled={editor.isCreating && editor.submittingForm}
                          onInput={(next) =>
                            editor.updateFormField('interval_minutes', next)}
                        />
                      </FormField>
                    {:else}
                      <FormField
                        controlId="cron-job-run-at"
                        label={t('cron.form.runAt', 'Run at')}
                        required
                      >
                        <TextField
                          id="cron-job-run-at"
                          type="datetime-local"
                          value={editor.formValues.run_at}
                          disabled={editor.isCreating && editor.submittingForm}
                          onInput={(next) =>
                            editor.updateFormField('run_at', next)}
                        />
                      </FormField>
                    {/if}

                    <FormField
                      controlId="cron-job-repeat"
                      label={t('cron.form.repeat', 'Repeat limit')}
                    >
                      <TextField
                        id="cron-job-repeat"
                        type="number"
                        min="1"
                        step="1"
                        value={editor.formValues.repeat}
                        placeholder={editor.isOnceSchedule
                          ? '1'
                          : t('cron.form.repeatPlaceholder', 'Unlimited')}
                        disabled={editor.isCreating && editor.submittingForm}
                        onInput={(next) =>
                          editor.updateFormField('repeat', next)}
                      />
                    </FormField>
                  </div>
                </section>

                <section class="cron-card">
                  <header class="cron-card-header">
                    <div>
                      <h2>{t('cron.sections.session', 'Session')}</h2>
                      <p>
                        {t(
                          'cron.sections.sessionSubtitle',
                          'Choose whether Runs share existing context.',
                        )}
                      </p>
                    </div>
                  </header>
                  <div class="cron-card-body">
                    <FormField controlId="cron-job-session">
                      {#snippet labelContent()}
                        {t('cron.form.sessionId', 'Session ID')}
                        <InfoHint
                          text={t(
                            'cron.form.sessionIdHelp',
                            'Optional: run inside one fixed existing session instead of a new one. Leave empty to let each run use its own.',
                          )}
                        />
                      {/snippet}
                      <TextField
                        id="cron-job-session"
                        value={editor.formValues.session_id}
                        placeholder={t(
                          'cron.form.sessionIdPlaceholder',
                          'Optional',
                        )}
                        disabled={editor.isCreating && editor.submittingForm}
                        onInput={(next) =>
                          editor.updateFormField('session_id', next)}
                      />
                    </FormField>

                    {#if !editor.isCreating && editor.selectedJob}
                      <details class="cron-technical-details">
                        <summary>
                          {t(
                            'cron.detail.technicalDetails',
                            'Technical details',
                          )}
                        </summary>
                        <div class="cron-technical-content">
                          <span class="cron-technical-label">
                            {t('cron.detail.scheduleId', 'Schedule ID')}
                          </span>
                          <code>{editor.selectedJob.id}</code>
                          <Button
                            variant="danger"
                            ariaLabel={t(
                              'cron.actions.deleteJob',
                              'Delete job {id}',
                              { id: editor.selectedJob.id },
                            )}
                            data-testid={`cron-delete-${editor.selectedJob.id}`}
                            disabled={editor.submittingForm ||
                              editor.mutatingJobId === editor.selectedJob.id}
                            onClick={() => editor.deleteJob(editor.selectedJob)}
                          >
                            {t('common.delete', 'Delete')}
                          </Button>
                        </div>
                      </details>
                    {/if}
                  </div>
                </section>
              </div>
            </div>

            {#if editor.formErrorMessage}
              <div class="cron-form-error">
                <Banner variant="error" role="alert">
                  {editor.formErrorMessage}
                </Banner>
              </div>
            {/if}

            <div class="cron-detail-footer">
              {#if editor.isCreating}
                <Button
                  variant="secondary"
                  disabled={editor.isCreating && editor.submittingForm}
                  onClick={editor.cancelCreate}
                >
                  {t('common.cancel', 'Cancel')}
                </Button>
              {/if}
              <Button
                variant={editor.isCreating ? 'primary' : 'tertiary'}
                type="submit"
                disabled={editor.isCreating && editor.submittingForm}
              >
                {editor.submittingForm
                  ? t('common.saving', 'Saving…')
                  : t('common.save', 'Save')}
              </Button>
            </div>
          </form>
        </div>
      {/key}
    {/if}
  </div>

  {#if editor.deleteConfirmJob}
    <ConfirmDialog
      title={t('cron.deleteConfirmTitle', 'Delete Scheduled Run')}
      body={t(
        'cron.deleteConfirm',
        'Delete this job permanently? It will no longer run.',
      )}
      confirmLabel={t('common.delete', 'Delete')}
      onConfirm={editor.confirmDeleteJob}
      onCancel={editor.cancelDeleteJob}
    />
  {/if}

  {#if editor.showDiscardConfirm}
    <ConfirmDialog
      title={t('cron.discardConfirmTitle', 'Discard unsaved changes?')}
      body={t(
        'cron.discardConfirm',
        'Your edits have not been saved. Discard them and continue?',
      )}
      confirmLabel={t('common.discard', 'Discard')}
      onConfirm={editor.confirmDiscard}
      onCancel={editor.cancelDiscard}
    />
  {/if}
</section>
