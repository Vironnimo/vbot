<script>
  import {
    displayValue,
    frequencyOptions,
    headerStatusVisible,
    intervalUnitOptions,
    listRowDetail,
    scheduleSummary,
    scheduleTechnicalValue,
    sessionSummary,
    lastResultSupport,
    remainingRunsLabel,
    scheduleRowDetails,
    statusLabel,
    statusChipVariant,
    outcomeLabel,
    isTerminalJob,
    weekdayOptions,
  } from './cron/presentation.js';

  import { t } from '$lib/i18n.js';
  import Button from './ui/Button.svelte';
  import Banner from './ui/Banner.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import StatusChip from './ui/StatusChip.svelte';
  import Toggle from './ui/Toggle.svelte';
  import {
    CRON_FREQUENCY_DAILY,
    CRON_FREQUENCY_HOURLY,
    CRON_FREQUENCY_MONTHLY,
    CRON_FREQUENCY_WEEKLY,
    CRON_STATUS_ACTIVE,
    applyAgentListResponse,
    applyCronListResponse,
    buildCronAgentDropdownOptions,
    buildCronAgentOptions,
    createCronViewState,
    visibleCronJobs,
  } from '$lib/cronView.js';
  import TextField from './ui/TextField.svelte';
  import Dropdown from './Dropdown.svelte';
  import TextArea from './ui/TextArea.svelte';
  import InfoHint from './ui/InfoHint.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import { onMount, untrack } from 'svelte';
  import { createStandaloneNavigation } from '$lib/navigation.svelte.js';
  import {
    listAgents,
    listCronJobs,
    listProjects,
    showProject,
  } from '$lib/api.js';
  import { createAgentTargetCatalogLoader } from '$lib/agentTargetOptions.js';
  import { createCronEditor, NEW_JOB_PLACE } from './cron/editor.svelte.js';

  const noop = () => {};

  let {
    // The place is the shown job, [job id], or ['new'] for a new job's form.
    // An empty place shows the first job.
    navigation = createStandaloneNavigation(),
    // The changed new job's form values, kept while the view is closed; null
    // without one.
    newJobDraft = $bindable(null),
    onToast = noop,
    serverUnavailable = false,
    cronRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
  } = $props();
  const editor = createCronEditor({
    get navigation() {
      return navigation;
    },
    get newJobDraft() {
      return newJobDraft;
    },
    set newJobDraft(value) {
      newJobDraft = value;
    },
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
  // Set once the first job and agent lists arrived, so the place can be
  // checked against them (a new job's form needs the agents).
  let jobsLoaded = $state(false);
  let agentsLoaded = $state(false);
  let placeReady = $derived(jobsLoaded && agentsLoaded);
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
  const scheduleFrequencies = frequencyOptions();
  const intervalUnits = intervalUnitOptions();
  const weekdays = weekdayOptions();
  // Frequencies that run at a time of day.
  const TIMED_FREQUENCIES = new Set([
    CRON_FREQUENCY_DAILY,
    CRON_FREQUENCY_WEEKLY,
    CRON_FREQUENCY_MONTHLY,
  ]);

  // Identity agents (bare ids, unchanged) plus project agents addressed as
  // `agent@projekt`. A project option's value IS the address, so saving sends it
  // straight through as the `agent_id` param. Group headers are inserted only
  // when project agents exist, so the identity-only dropdown is unchanged.
  let agentOptions = $derived(
    buildCronAgentDropdownOptions(viewState.agents, projectTeams, {
      identityGroupLabel: t('cron.form.agentGroup.identity'),
      projectGroupLabel: t('cron.form.agentGroup.project'),
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

  // Place -> shown job. Until the lists arrived the target waits; then an
  // empty place shows the first job, an unknown one keeps the shown job, and
  // the entry is corrected to what is shown.
  $effect(() => {
    const target = navigation.place[0] ?? '';
    if (!placeReady) return;
    untrack(() => {
      showPlace(target);
      showJobInPlace();
    });
  });

  // A reloaded list without the shown job shows the first job instead. An
  // empty list keeps it, so a job saved a moment ago survives a reload that
  // does not name it yet.
  $effect(() => {
    void jobs;
    if (!placeReady) return;
    untrack(() => {
      if (editor.isCreating || jobs.length === 0) return;
      if (!jobs.some((job) => job.id === editor.selectedJobId)) {
        editor.selectJobNow(jobs[0]);
      }
    });
  });

  // Shown job -> place: saving turned the new job into a real one, or a
  // reload or deletion showed another job.
  $effect(() => {
    void editor.isCreating;
    void editor.selectedJobId;
    if (!placeReady) return;
    untrack(showJobInPlace);
  });

  function showPlace(target) {
    if (target === NEW_JOB_PLACE && hasAgents) {
      if (!editor.isCreating) editor.startCreateNow();
      return;
    }
    // A job saved a moment ago is shown before the list names it.
    if (target && !editor.isCreating && target === editor.selectedJobId) {
      return;
    }
    const listed = (id) => jobs.find((job) => job.id === id);
    const job =
      listed(target) ??
      (target && !editor.isCreating ? listed(editor.selectedJobId) : null) ??
      jobs[0];
    if (!job) {
      editor.isCreating = false;
      editor.selectedJobId = '';
    } else if (editor.isCreating || job.id !== editor.selectedJobId) {
      editor.selectJobNow(job);
    }
  }

  function showJobInPlace() {
    const shown = editor.isCreating ? NEW_JOB_PLACE : editor.selectedJobId;
    if ((navigation.place[0] ?? '') !== shown) {
      navigation.replace(shown ? [shown] : []);
    }
  }

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

      viewState.agentsError = `${t('cron.errors.loadAgents')} ${errorMessageText(error, t('common.unknown'))}`;
    } finally {
      if (!destroyed && requestId === agentsRequestId) {
        viewState.loadingAgents = false;
        agentsLoaded = true;
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
      jobsLoaded = true;
    } catch (error) {
      if (destroyed || requestId !== jobsRequestId) {
        return;
      }

      viewState.jobsError = `${t('cron.errors.loadJobs')} ${errorMessageText(error, t('common.unknown'))}`;
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
    return agentLabelByValue.get(target) || target || t('common.unknown');
  }

  // Route a message to the app-level toast stack. Error toasts are sticky by
  // default at the app level; when an error object is passed its message is
  // appended so transport failures stay diagnosable.
  function showToast(title, variant = 'success', error = null) {
    const message =
      variant === 'error' && error
        ? errorMessageText(error, t('common.unknown'))
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
          {t('cron.title')}
        </span>
        <div class="pane-header-actions">
          <Button
            variant="tertiary"
            icon
            ariaLabel={t('cron.detail.createTitle')}
            tooltip={t('cron.detail.createTitle')}
            disabled={!hasAgents}
            onClick={editor.startCreate}
          >
            <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
              <path d="M7 1.5v11M1.5 7h11" />
            </svg>
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
              {t('common.retry')}
            </Button>
          </div>
        {:else if !serverUnavailable && !hasAgents && !viewState.loadingAgents}
          <p class="cron-list-state cron-list-state--warn" role="status">
            {t('cron.noAgents')}
          </p>
        {/if}

        {#if isLoading && !serverUnavailable}
          <p class="cron-list-state" role="status">
            {t('cron.loading')}
          </p>
        {:else if viewState.jobsError && !serverUnavailable}
          <div class="cron-load-error">
            <Banner variant="error" role="alert">
              {viewState.jobsError}
            </Banner>
            <Button variant="secondary" onClick={() => loadJobs()}>
              {t('common.retry')}
            </Button>
          </div>
        {:else if jobs.length === 0}
          <EmptyState density="compact" description={t('cron.emptyTitle')} />
        {:else}
          <ul class="cron-list" aria-label={t('cron.list.ariaLabel')}>
            {#each jobs as job (job.id)}
              <li>
                <button
                  type="button"
                  class="cron-item secondary-list__item"
                  class:active={!editor.isCreating &&
                    job.id === editor.selectedJobId}
                  data-testid={`cron-item-${job.id}`}
                  use:tooltip={() => scheduleRowDetails(job, { agentLabel })}
                  onclick={() => editor.selectJob(job)}
                >
                  <span class="cron-item-inner">
                    <span class="cron-item-head">
                      <span
                        class="cron-status-dot cron-status-dot--{statusChipVariant(
                          job,
                        )}"
                        role="img"
                        aria-label={statusLabel(job.status)}
                      ></span>
                      <span class="cron-item-name">
                        {job.name}
                      </span>
                    </span>
                    <span class="cron-item-detail">
                      {listRowDetail(job, viewState.systemTimezone)}
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
          title={jobs.length === 0
            ? t('cron.emptyDetailTitle')
            : t('cron.selectTitle')}
          description={!serverUnavailable && !hasAgents
            ? t('cron.noAgents')
            : t('cron.emptySubtitle')}
        >
          {#snippet actions()}
            <Button
              variant={jobs.length === 0 ? 'primary' : 'secondary'}
              disabled={!hasAgents}
              onClick={editor.startCreate}
            >
              {t('cron.detail.createTitle')}
            </Button>
          {/snippet}
        </EmptyState>
      </div>
    {:else}
      {#key editor.isCreating ? 'cron-create' : editor.selectedJobId}
        <div class="cron-detail-pane">
          <form class="cron-detail-scroll" onsubmit={editor.submitForm}>
            <div class="detail-top">
              <div>
                <div class="detail-heading">{editor.detailTitle}</div>
                {#if editor.isCreating}
                  <div class="detail-sub">
                    {t('cron.detail.createSubtitle')}
                  </div>
                {/if}
              </div>

              <div class="detail-btns">
                {#if !editor.isCreating && editor.selectedJob}
                  <!-- The Enabled switch already says active or paused; the
                       chip only names states the switch cannot. -->
                  {#if headerStatusVisible(editor.selectedJob)}
                    <StatusChip variant={statusChipVariant(editor.selectedJob)}>
                      {statusLabel(editor.selectedJob.status)}
                    </StatusChip>
                  {/if}
                  {#if !isTerminalJob(editor.selectedJob)}
                    <label class="cron-enabled-control">
                      <span>{t('cron.actions.enabled')}</span>
                      <Toggle
                        checked={editor.selectedJob.status ===
                          CRON_STATUS_ACTIVE}
                        ariaLabel={editor.selectedJob.status ===
                        CRON_STATUS_ACTIVE
                          ? t('cron.actions.disableJob', {
                              id: editor.selectedJob.id,
                            })
                          : t('cron.actions.enableJob', {
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
              <div class="s-group cron-overview">
                <div class="cron-summary" aria-label={t('cron.detail.summary')}>
                  <div class="cron-summary-item">
                    <span class="cron-summary-label">
                      {t('cron.detail.nextFire')}
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
                      {t('cron.detail.cadence')}
                    </span>
                    <strong class="cron-summary-value cron-summary-value--wrap">
                      {scheduleSummary(editor.selectedJob)}
                    </strong>
                    {#if scheduleTechnicalValue(editor.selectedJob)}
                      <span
                        class="cron-summary-support"
                        class:cron-summary-support--mono={editor.selectedJob
                          .schedule_type === 'cron'}
                      >
                        {scheduleTechnicalValue(editor.selectedJob)}
                      </span>
                    {/if}
                  </div>
                  <div class="cron-summary-item">
                    <span class="cron-summary-label">
                      {t('cron.detail.lastResult')}
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
                      {t('cron.detail.target')}
                    </span>
                    <strong class="cron-summary-value">
                      {agentLabel(editor.selectedJob.agent_id)}
                    </strong>
                    <span class="cron-summary-support">
                      {sessionSummary(editor.selectedJob)}
                    </span>
                  </div>
                </div>

                <details class="s-disclosure cron-execution-details">
                  <summary>
                    {t('cron.detail.executionDetails')}
                  </summary>
                  <dl class="s-disclosure__body cron-execution-grid">
                    <div>
                      <dt>{t('cron.detail.lastAttempt')}</dt>
                      <dd>
                        {displayValue(
                          editor.selectedJob.last_attempt_at_display,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>{t('cron.detail.lastFired')}</dt>
                      <dd>
                        {displayValue(editor.selectedJob.last_fired_at_display)}
                      </dd>
                    </div>
                    <div>
                      <dt>
                        {t('cron.detail.lastCompleted')}
                      </dt>
                      <dd>
                        {displayValue(
                          editor.selectedJob.last_completed_at_display,
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>{t('cron.detail.lastRun')}</dt>
                      <dd class="cron-execution-grid__id">
                        {displayValue(editor.selectedJob.last_run_id)}
                      </dd>
                    </div>
                    <div>
                      <dt>{t('cron.detail.failures')}</dt>
                      <dd>{editor.selectedJob.consecutive_failures}</dd>
                    </div>
                    <div>
                      <dt>
                        {t('cron.detail.remainingRuns')}
                      </dt>
                      <dd>{remainingRunsLabel(editor.selectedJob)}</dd>
                    </div>
                    <div>
                      <dt>{t('cron.detail.scheduleId')}</dt>
                      <dd class="cron-execution-grid__id">
                        {editor.selectedJob.id}
                      </dd>
                    </div>
                  </dl>
                </details>
              </div>

              {#if editor.selectedJob.last_error}
                <Banner variant="error" role="status">
                  {editor.selectedJob.last_error}
                </Banner>
              {/if}
            {/if}

            <section
              class="s-section cron-task-section"
              aria-labelledby="cron-section-task"
            >
              <header class="s-section__head">
                <h3 class="s-section__title" id="cron-section-task">
                  {t('cron.sections.task')}
                </h3>
              </header>
              <p class="s-section__desc">
                {t('cron.sections.taskSubtitle')}
              </p>
              <div class="s-section__body">
                <div class="s-group">
                  <div class="s-row">
                    <div class="s-row-info">
                      <label class="s-row-label" for="cron-job-name">
                        {t('cron.form.name')}
                      </label>
                    </div>
                    <div class="s-row-control">
                      <TextField
                        id="cron-job-name"
                        value={editor.formValues.name}
                        placeholder={t('cron.form.namePlaceholder')}
                        disabled={editor.isCreating && editor.submittingForm}
                        onInput={(value) =>
                          editor.updateFormField('name', value)}
                      />
                    </div>
                  </div>

                  <div class="s-row">
                    <div class="s-row-info">
                      <label class="s-row-label" for="cron-form-agent">
                        {t('cron.form.agent')}
                        <span class="cron-required" aria-hidden="true">*</span>
                      </label>
                    </div>
                    <div class="s-row-control">
                      <Dropdown
                        id="cron-form-agent"
                        value={editor.formValues.agent_id}
                        options={agentOptions}
                        placeholder={t('cron.form.agentPlaceholder')}
                        ariaLabel={t('cron.form.agent')}
                        disabled={!hasAgents || editor.submittingForm}
                        triggerClass="cron-dropdown"
                        listClass="cron-dropdown-list"
                        onValueChange={(value) =>
                          editor.updateFormField('agent_id', value)}
                      />
                    </div>
                  </div>

                  <div class="s-row s-row--stacked cron-prompt-row">
                    <label class="s-row-label" for="cron-job-prompt">
                      {t('cron.form.prompt')}
                      <span class="cron-required" aria-hidden="true">*</span>
                    </label>
                    <TextArea
                      id="cron-job-prompt"
                      class="cron-prompt-editor"
                      value={editor.formValues.prompt}
                      rows={8}
                      placeholder={t('cron.form.promptPlaceholder')}
                      disabled={editor.isCreating && editor.submittingForm}
                      onInput={(value) =>
                        editor.updateFormField('prompt', value)}
                    />
                  </div>
                </div>
              </div>
            </section>

            <section class="s-section" aria-labelledby="cron-section-timing">
              <header class="s-section__head">
                <h3 class="s-section__title" id="cron-section-timing">
                  {t('cron.sections.timing')}
                </h3>
              </header>
              <p class="s-section__desc">
                {t('cron.sections.timingSubtitle')}
              </p>
              <div class="s-section__body">
                <div class="s-group">
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
                        onValueChange={(frequency) =>
                          editor.updateSchedule({ frequency })}
                      />
                    </div>
                  </div>

                  {#if editor.isOnceSchedule}
                    <div class="s-row">
                      <div class="s-row-info">
                        <label class="s-row-label" for="cron-job-run-at">
                          {t('cron.form.runAt')}
                          <span class="cron-required" aria-hidden="true">*</span
                          >
                        </label>
                        <div class="s-row-desc">
                          {t('cron.form.timezoneNote', {
                            timezone: viewState.systemTimezone,
                          })}
                        </div>
                      </div>
                      <div class="s-row-control">
                        <TextField
                          id="cron-job-run-at"
                          type="datetime-local"
                          value={editor.formValues.run_at}
                          disabled={editor.isCreating && editor.submittingForm}
                          onInput={(next) =>
                            editor.updateFormField('run_at', next)}
                        />
                      </div>
                    </div>
                  {:else if editor.isIntervalSchedule}
                    <div class="s-row">
                      <div class="s-row-info">
                        <label class="s-row-label" for="cron-job-interval">
                          {t('cron.form.interval')}
                          <span class="cron-required" aria-hidden="true">*</span
                          >
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
                          <span class="cron-required" aria-hidden="true">*</span
                          >
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
                            disabled={editor.isCreating &&
                              editor.submittingForm}
                            onInput={(minute) =>
                              editor.updateSchedule({ minute })}
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
                                disabled={editor.isCreating &&
                                  editor.submittingForm}
                                onclick={() =>
                                  editor.toggleWeekday(weekday.day)}
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
                            disabled={editor.isCreating &&
                              editor.submittingForm}
                            onInput={(month_day) =>
                              editor.updateSchedule({ month_day })}
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
                              timezone: viewState.systemTimezone,
                            })}
                          </div>
                        </div>
                        <div class="s-row-control s-row-control--number">
                          <TextField
                            id="cron-job-time"
                            type="time"
                            value={editor.formValues.time}
                            disabled={editor.isCreating &&
                              editor.submittingForm}
                            onInput={(time) => editor.updateSchedule({ time })}
                          />
                        </div>
                      </div>
                    {/if}
                  {/if}

                  {#if !editor.isOnceSchedule}
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
                          onInput={(next) =>
                            editor.updateFormField('repeat', next)}
                        />
                      </div>
                    </div>
                  {/if}
                </div>
              </div>
            </section>

            <section class="s-section" aria-labelledby="cron-section-session">
              <header class="s-section__head">
                <h3 class="s-section__title" id="cron-section-session">
                  {t('cron.sections.session')}
                </h3>
              </header>
              <p class="s-section__desc">
                {t('cron.sections.sessionSubtitle')}
              </p>
              <div class="s-section__body">
                <div class="s-group">
                  <div class="s-row">
                    <div class="s-row-info">
                      <label
                        class="s-row-label cron-label-with-hint"
                        for="cron-job-session"
                      >
                        {t('cron.form.sessionId')}
                        <InfoHint text={t('cron.form.sessionIdHelp')} />
                      </label>
                    </div>
                    <div class="s-row-control">
                      <TextField
                        id="cron-job-session"
                        value={editor.formValues.session_id}
                        placeholder={t('cron.form.sessionIdPlaceholder')}
                        disabled={editor.isCreating && editor.submittingForm}
                        onInput={(next) =>
                          editor.updateFormField('session_id', next)}
                      />
                    </div>
                  </div>
                </div>
              </div>
            </section>

            <!-- Identifier and deletion close the page, after everything the
                 user edits. -->
            {#if !editor.isCreating && editor.selectedJob}
              <div class="s-group cron-technical-group">
                <details class="s-disclosure cron-technical-details">
                  <summary>
                    {t('cron.detail.technicalDetails')}
                  </summary>
                  <div class="s-disclosure__body cron-technical-content">
                    <div class="cron-technical-id">
                      <span class="cron-technical-label">
                        {t('cron.detail.scheduleId')}
                      </span>
                      <code>{editor.selectedJob.id}</code>
                    </div>
                    <Button
                      variant="danger"
                      ariaLabel={t('cron.actions.deleteJob', {
                        id: editor.selectedJob.id,
                      })}
                      data-testid={`cron-delete-${editor.selectedJob.id}`}
                      disabled={editor.submittingForm ||
                        editor.mutatingJobId === editor.selectedJob.id}
                      onClick={() => editor.deleteJob(editor.selectedJob)}
                    >
                      {t('common.delete')}
                    </Button>
                  </div>
                </details>
              </div>
            {/if}

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
                  {t('common.cancel')}
                </Button>
              {/if}
              <Button
                variant={editor.isCreating ? 'primary' : 'tertiary'}
                type="submit"
                disabled={editor.isCreating && editor.submittingForm}
              >
                {editor.submittingForm ? t('common.saving') : t('common.save')}
              </Button>
            </div>
          </form>
        </div>
      {/key}
    {/if}
  </div>

  {#if editor.deleteConfirmJob}
    <ConfirmDialog
      title={t('cron.deleteConfirmTitle')}
      body={t('cron.deleteConfirm')}
      confirmLabel={t('common.delete')}
      onConfirm={editor.confirmDeleteJob}
      onCancel={editor.cancelDeleteJob}
    />
  {/if}

  {#if editor.showDiscardConfirm}
    <ConfirmDialog
      title={t('cron.discardConfirmTitle')}
      body={t('cron.discardConfirm')}
      confirmLabel={t('common.discard')}
      onConfirm={editor.confirmDiscard}
      onCancel={editor.cancelDiscard}
    />
  {/if}
</section>
