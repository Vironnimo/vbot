<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import FormField from './ui/FormField.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import TextArea from './ui/TextArea.svelte';
  import TextField from './ui/TextField.svelte';
  import Toggle from './ui/Toggle.svelte';
  import {
    createCronJob,
    deleteCronJob,
    disableCronJob,
    enableCronJob,
    listAgents,
    listCronJobs,
    listProjects,
    showProject,
    updateCronJob,
  } from '$lib/api.js';
  import {
    applyAgentListResponse,
    applyCronListResponse,
    buildCreateCronPayload,
    buildCronAgentDropdownOptions,
    buildCronAgentOptions,
    buildCronPresetOptions,
    buildUpdateCronPayload,
    createCronFormValues,
    createCronViewState,
    CRON_PRESET_CUSTOM,
    CRON_SCHEDULE_TYPE_CRON,
    CRON_SCHEDULE_TYPE_INTERVAL,
    CRON_SCHEDULE_TYPE_ONCE,
    CRON_STATUS_ACTIVE,
    CRON_STATUS_COMPLETED,
    CRON_STATUS_MISSED,
    cronFormFingerprint,
    cronPresetExpression,
    cronPresetForExpression,
    describeCronExpression,
    projectIdsFromList,
    projectTeamEntry,
    visibleCronJobs,
  } from '$lib/cronView.js';
  import { t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
  import InfoHint from './ui/InfoHint.svelte';

  const noop = () => {};
  const initialFormValues = createCronFormValues();

  let {
    onToast = noop,
    serverUnavailable = false,
    cronRefreshToken = 0,
    agentsRefreshToken = 0,
    projectsRefreshToken = 0,
    targetJobId = '',
  } = $props();

  let viewState = $state(createCronViewState());

  // The detail pane edits either an existing job (a selected id) or a fresh
  // create draft (`isCreating`). Its form + validation state is panel-local; the
  // list is the master. Selecting a row, or starting a job, reseeds these.
  let selectedJobId = $state('');
  let isCreating = $state(false);
  let formValues = $state(initialFormValues);
  let formBaseline = $state(cronFormFingerprint(initialFormValues));
  let selectedPreset = $state(CRON_PRESET_CUSTOM);
  let formErrorMessage = $state('');
  let submittingForm = $state(false);
  // The id of the job whose enable/disable/delete mutation is in flight, so its
  // controls disable without freezing the whole pane.
  let mutatingJobId = $state('');
  // The cron job awaiting delete confirmation (null = dialog closed).
  let deleteConfirmJob = $state(null);
  let pendingDiscardAction = null;
  let showDiscardConfirm = $state(false);
  let pendingJobsResult = null;

  // Project teams power the project-agent options in the cron dropdown. They are
  // loaded lazily the first time the detail pane renders a form and cached for
  // the lifetime of the view, so the N+1 `project.show` scan (one per project)
  // never runs on every render — only once, on demand.
  let projectTeams = $state([]);
  let projectTeamsLoaded = false;
  let projectTeamsRequestId = 0;

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
  let selectedJob = $derived(
    jobs.find((job) => job.id === selectedJobId) ?? null,
  );
  // The detail form is shown when creating, or when a real job is selected.
  let showDetailForm = $derived(isCreating || Boolean(selectedJob));
  let isDirty = $derived(
    showDetailForm && cronFormFingerprint(formValues) !== formBaseline,
  );
  let isCronSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_CRON,
  );
  let isIntervalSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL,
  );
  let isOnceSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_ONCE,
  );
  let cronExpressionPreview = $derived(
    describeCronExpression(formValues.cron_expression),
  );
  let detailTitle = $derived(
    isCreating
      ? t('cron.detail.createTitle', 'Create Scheduled Run')
      : selectedJob?.name || t('cron.detail.editTitle', 'Edit Scheduled Run'),
  );
  let presetOptions = $derived(
    buildCronPresetOptions((key) =>
      t(`cron.presets.${key}`, key === CRON_PRESET_CUSTOM ? 'Custom' : key),
    ),
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
    };
  });

  // Auto-select the first job once the list loads, unless the user is mid-create
  // or already has a selection that still exists. A targetJobId (calendar deep
  // link) wins once; it is consumed so the user can freely change selection.
  let appliedTargetJobId = $state('');
  $effect(() => {
    if (isCreating || jobs.length === 0) {
      return;
    }
    if (
      targetJobId &&
      targetJobId !== appliedTargetJobId &&
      jobs.some((job) => job.id === targetJobId)
    ) {
      appliedTargetJobId = targetJobId;
      selectJobNow(jobs.find((job) => job.id === targetJobId));
      return;
    }
    if (!jobs.some((job) => job.id === selectedJobId) && !isDirty) {
      selectJobNow(jobs[0]);
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

      if (options.external === true && isDirty) {
        pendingJobsResult = result;
        return;
      }
      pendingJobsResult = null;
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
    if (projectTeamsLoaded) {
      return;
    }

    const requestId = projectTeamsRequestId + 1;
    projectTeamsRequestId = requestId;

    try {
      const listResult = await listProjects();
      if (destroyed || requestId !== projectTeamsRequestId) {
        return;
      }

      const projectIds = projectIdsFromList(listResult);
      const showResults = await Promise.all(
        projectIds.map((projectId) =>
          showProject(projectId)
            .then((showResult) => projectTeamEntry(projectId, showResult))
            .catch(() => null),
        ),
      );
      if (destroyed || requestId !== projectTeamsRequestId) {
        return;
      }

      projectTeams = showResults.filter((entry) => entry !== null);
      projectTeamsLoaded = true;
    } catch {
      // Identity agents remain available; leave projectTeams empty and allow a
      // retry on the next render (projectTeamsLoaded stays false).
      if (!destroyed && requestId === projectTeamsRequestId) {
        projectTeams = [];
      }
    }
  }

  function selectJob(job) {
    if (!job?.id) {
      return;
    }
    if (!isCreating && job.id === selectedJobId) {
      return;
    }
    requestFormTransition(() => selectJobNow(job));
  }

  function selectJobNow(job) {
    isCreating = false;
    selectedJobId = job.id;
    formValues = createCronFormValues(job, viewState.systemTimezone);
    if (!formValues.agent_id) {
      formValues.agent_id = viewState.agents[0]?.id ?? '';
    }
    selectedPreset = cronPresetForExpression(formValues.cron_expression);
    formBaseline = cronFormFingerprint(formValues);
    formErrorMessage = '';
    loadProjectTeams();
  }

  function startCreate() {
    requestFormTransition(startCreateNow);
  }

  function startCreateNow() {
    isCreating = true;
    formValues = createCronFormValues(null, viewState.systemTimezone);
    formValues.agent_id = viewState.agents[0]?.id ?? '';
    selectedPreset = CRON_PRESET_CUSTOM;
    formBaseline = cronFormFingerprint(formValues);
    formErrorMessage = '';
    loadProjectTeams();
  }

  // Cancel a create draft and return to the previously selected job (if any).
  function cancelCreate() {
    if (submittingForm) {
      return;
    }
    requestFormTransition(() => {
      isCreating = false;
      if (selectedJob) {
        selectJobNow(selectedJob);
      } else if (jobs.length > 0) {
        selectJobNow(jobs[0]);
      }
    });
  }

  function requestFormTransition(action) {
    if (!isDirty) {
      action();
      return;
    }
    pendingDiscardAction = action;
    showDiscardConfirm = true;
  }

  function cancelDiscard() {
    pendingDiscardAction = null;
    showDiscardConfirm = false;
  }

  function confirmDiscard() {
    const action = pendingDiscardAction;
    pendingDiscardAction = null;
    showDiscardConfirm = false;
    if (pendingJobsResult) {
      applyCronListResponse(viewState, pendingJobsResult);
      pendingJobsResult = null;
    }
    action?.();
  }

  function setScheduleType(scheduleType) {
    formValues.schedule_type = scheduleType;
    formErrorMessage = '';
  }

  function updateFormField(fieldName, value) {
    formValues[fieldName] = value;
    formErrorMessage = '';
  }

  // Selecting a preset fills its expression; the field stays editable and the
  // live preview keeps working. Custom (or an unknown key) fills nothing.
  function applyPreset(presetKey) {
    selectedPreset = presetKey;
    if (presetKey === CRON_PRESET_CUSTOM) {
      return;
    }
    const expression = cronPresetExpression(presetKey);
    if (expression) {
      formValues.cron_expression = expression;
    }
    formErrorMessage = '';
  }

  // Hand-editing the expression re-derives the preset selection, flipping it to
  // Custom when the text no longer matches the chosen preset.
  function updateCronExpression(value) {
    formValues.cron_expression = value;
    selectedPreset = cronPresetForExpression(value);
    formErrorMessage = '';
  }

  function validateFormValues() {
    const hasCoreValues =
      formValues.agent_id.trim().length > 0 &&
      formValues.prompt.trim().length > 0;
    let hasScheduleValue = formValues.run_at.trim().length > 0;
    if (isCronSchedule) {
      hasScheduleValue = formValues.cron_expression.trim().length > 0;
    } else if (isIntervalSchedule) {
      const intervalMinutes = Number(formValues.interval_minutes);
      hasScheduleValue =
        Number.isInteger(intervalMinutes) && intervalMinutes > 0;
    }
    const repeat = formValues.repeat.trim();
    const repeatValue = Number(repeat);
    const hasValidRepeat =
      (repeat.length === 0 && (isCreating || !isOnceSchedule)) ||
      (Number.isInteger(repeatValue) &&
        repeatValue > 0 &&
        (!isOnceSchedule || repeatValue === 1));

    if (!hasCoreValues || !hasScheduleValue || !hasValidRepeat) {
      formErrorMessage = t(
        'cron.errors.missingRequired',
        'Agent, prompt, and valid schedule details are required. Repeat must be a positive integer; one-time schedules allow only 1.',
      );
      return false;
    }

    return true;
  }

  async function submitForm(event) {
    event.preventDefault();

    if (submittingForm || !validateFormValues()) {
      return;
    }

    const creating = isCreating;
    submittingForm = true;
    formErrorMessage = '';

    try {
      let targetJobId = selectedJobId;
      if (creating) {
        const created = await createCronJob(buildCreateCronPayload(formValues));
        targetJobId = typeof created?.id === 'string' ? created.id : '';
        if (targetJobId) {
          formValues.id = targetJobId;
        }
        showToast(t('cron.messages.created', 'Cron job created.'));
      } else {
        await updateCronJob(buildUpdateCronPayload(formValues));
        showToast(t('cron.messages.updated', 'Cron job updated.'));
      }

      if (destroyed) {
        return;
      }

      isCreating = false;
      if (targetJobId) {
        selectedJobId = targetJobId;
      }
      await loadJobs({ silent: true });
      const savedJob = jobs.find((job) => job.id === targetJobId);
      if (savedJob) {
        selectJobNow(savedJob);
      } else {
        formBaseline = cronFormFingerprint(formValues);
      }
    } catch (error) {
      formErrorMessage = `${t('cron.errors.save', 'Cron job could not be saved.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      if (!destroyed) {
        submittingForm = false;
      }
    }
  }

  async function toggleJob(job) {
    if (
      !job?.id ||
      job.status === CRON_STATUS_COMPLETED ||
      job.status === CRON_STATUS_MISSED
    ) {
      return;
    }

    mutatingJobId = job.id;

    try {
      if (job.status === CRON_STATUS_ACTIVE) {
        await disableCronJob(job.id);
        showToast(t('cron.messages.disabled', 'Cron job disabled.'));
      } else {
        await enableCronJob(job.id);
        showToast(t('cron.messages.enabled', 'Cron job enabled.'));
      }

      await loadJobs({ silent: true });
    } catch (error) {
      showToast(
        t('cron.errors.toggle', 'Cron job status could not be updated.'),
        'error',
        error,
      );
    } finally {
      mutatingJobId = '';
    }
  }

  function deleteJob(job) {
    if (!job?.id) {
      return;
    }
    deleteConfirmJob = job;
  }

  function cancelDeleteJob() {
    deleteConfirmJob = null;
  }

  async function confirmDeleteJob() {
    const job = deleteConfirmJob;
    deleteConfirmJob = null;
    if (!job?.id) {
      return;
    }

    mutatingJobId = job.id;

    try {
      await deleteCronJob(job.id);
      showToast(t('cron.messages.deleted', 'Cron job deleted.'));
      if (selectedJobId === job.id) {
        selectedJobId = '';
      }
      await loadJobs({ silent: true });
    } catch (error) {
      showToast(
        t('cron.errors.delete', 'Cron job could not be deleted.'),
        'error',
        error,
      );
    } finally {
      mutatingJobId = '';
    }
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

  function displayValue(value) {
    return value || t('cron.notAvailable', '—');
  }

  function listNextRun(job) {
    if (!job?.next_fire_at_display) {
      return t('cron.list.noNextRun', 'No next Run');
    }

    return t('cron.list.nextRun', 'Next · {time}', {
      time: job.next_fire_at_display,
    });
  }

  function scheduleKindLabel(job) {
    if (job?.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
      return t('cron.detail.kind.once', 'One-time schedule');
    }

    return t('cron.detail.kind.recurring', 'Recurring schedule');
  }

  function scheduleSummary(job) {
    if (!job) {
      return t('cron.notAvailable', '—');
    }

    if (job.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
      return t('cron.form.scheduleType.once', 'Once');
    }
    if (job.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
      return displayValue(job.schedule_description);
    }

    return (
      describeCronExpression(job.cron_expression) ||
      displayValue(job.schedule_description)
    );
  }

  function scheduleTechnicalValue(job) {
    if (job?.schedule_type === CRON_SCHEDULE_TYPE_ONCE) {
      return displayValue(job.run_at ? job.schedule_description : '');
    }
    if (job?.schedule_type === CRON_SCHEDULE_TYPE_INTERVAL) {
      return displayValue(job.schedule_description);
    }

    return displayValue(job?.cron_expression);
  }

  function sessionSummary(job) {
    return job?.session_id
      ? job.session_id
      : t('cron.detail.newSessionEachRun', 'New Session each Run');
  }

  function lastResultSupport(job) {
    if (!job?.last_completed_at_display) {
      return t('cron.detail.waitingForFirstRun', 'Waiting for first execution');
    }

    return job.last_completed_at_display;
  }

  function remainingRunsLabel(job) {
    return job?.remaining_runs === null
      ? t('cron.detail.unlimited', 'Unlimited')
      : String(job?.remaining_runs ?? 0);
  }

  function statusLabel(status) {
    if (status === CRON_STATUS_ACTIVE) {
      return t('cron.status.active', 'Active');
    }

    if (status === 'paused') {
      return t('cron.status.paused', 'Paused');
    }

    if (status === 'failed') {
      return t('cron.status.failed', 'Failed');
    }

    if (status === CRON_STATUS_MISSED) {
      return t('cron.status.missed', 'Missed');
    }

    return t('cron.status.completed', 'Completed');
  }

  function statusChipVariant(job) {
    if (
      job.status === CRON_STATUS_ACTIVE &&
      job.last_outcome !== 'failed' &&
      job.last_outcome !== 'cancelled'
    ) {
      return 'success';
    }

    if (job.status === 'paused' || job.status === CRON_STATUS_MISSED) {
      return 'warn';
    }

    if (
      job.status === 'failed' ||
      job.last_outcome === 'failed' ||
      job.last_outcome === 'cancelled'
    ) {
      return 'error';
    }

    return 'neutral';
  }

  function outcomeLabel(outcome) {
    if (outcome === 'success') {
      return t('cron.outcome.success', 'Succeeded');
    }
    if (outcome === 'failed') {
      return t('cron.outcome.failed', 'Failed');
    }
    if (outcome === 'cancelled') {
      return t('cron.outcome.cancelled', 'Cancelled');
    }
    if (outcome === 'missed') {
      return t('cron.outcome.missed', 'Missed');
    }
    if (outcome === 'unknown') {
      return t('cron.outcome.unknown', 'Outcome unknown after restart');
    }
    return t('cron.notAvailable', '—');
  }

  function isTerminalJob(job) {
    return (
      job?.status === CRON_STATUS_COMPLETED ||
      job?.status === CRON_STATUS_MISSED
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
          <Button variant="primary" disabled={!hasAgents} onClick={startCreate}>
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
                  class:active={!isCreating && job.id === selectedJobId}
                  data-testid={`cron-item-${job.id}`}
                  onclick={() => selectJob(job)}
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

    {#if !showDetailForm}
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
      {#key isCreating ? 'cron-create' : selectedJobId}
        <div class="cron-detail-pane">
          <form class="cron-detail-scroll" onsubmit={submitForm}>
            <div class="detail-top">
              <div>
                <div class="detail-eyebrow">
                  {isCreating
                    ? t('cron.detail.kind.new', 'New schedule')
                    : scheduleKindLabel(selectedJob)}
                </div>
                <div class="detail-heading">{detailTitle}</div>
                <div class="detail-sub">
                  {isCreating
                    ? t(
                        'cron.detail.createSubtitle',
                        'Define the task, timing, and Session for a new scheduled Run.',
                      )
                    : t('cron.detail.subtitle', '{schedule} with {agent}.', {
                        schedule: scheduleKindLabel(selectedJob),
                        agent: agentLabel(selectedJob?.agent_id),
                      })}
                </div>
              </div>

              <div class="detail-btns">
                {#if !isCreating && selectedJob}
                  <StatusChip variant={statusChipVariant(selectedJob)}>
                    {statusLabel(selectedJob.status)}
                  </StatusChip>
                  {#if !isTerminalJob(selectedJob)}
                    <label class="cron-enabled-control">
                      <span>{t('cron.actions.enabled', 'Enabled')}</span>
                      <Toggle
                        checked={selectedJob.status === CRON_STATUS_ACTIVE}
                        ariaLabel={selectedJob.status === CRON_STATUS_ACTIVE
                          ? t('cron.actions.disableJob', 'Disable job {id}', {
                              id: selectedJob.id,
                            })
                          : t('cron.actions.enableJob', 'Enable job {id}', {
                              id: selectedJob.id,
                            })}
                        disabled={submittingForm ||
                          mutatingJobId === selectedJob.id}
                        data-testid={`cron-toggle-${selectedJob.id}`}
                        onChange={() => toggleJob(selectedJob)}
                      />
                    </label>
                  {/if}
                {/if}
              </div>
            </div>

            {#if !isCreating && selectedJob}
              <div
                class="cron-summary"
                aria-label={t('cron.detail.summary', 'Schedule summary')}
              >
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.nextFire', 'Next Run')}
                  </span>
                  <strong class="cron-summary-value">
                    {displayValue(selectedJob.next_fire_at_display)}
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
                    {scheduleSummary(selectedJob)}
                  </strong>
                  <span class="cron-summary-support cron-summary-support--mono">
                    {scheduleTechnicalValue(selectedJob)}
                  </span>
                </div>
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.lastResult', 'Last result')}
                  </span>
                  <strong class="cron-summary-value">
                    {outcomeLabel(selectedJob.last_outcome)}
                  </strong>
                  <span class="cron-summary-support">
                    {lastResultSupport(selectedJob)}
                  </span>
                </div>
                <div class="cron-summary-item">
                  <span class="cron-summary-label">
                    {t('cron.detail.target', 'Target')}
                  </span>
                  <strong class="cron-summary-value">
                    {agentLabel(selectedJob.agent_id)}
                  </strong>
                  <span class="cron-summary-support cron-summary-support--mono">
                    {sessionSummary(selectedJob)}
                  </span>
                </div>
              </div>

              {#if selectedJob.last_error}
                <Banner variant="error" role="status">
                  {selectedJob.last_error}
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
                      {displayValue(selectedJob.last_attempt_at_display)}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.lastFired', 'Last fired')}</dt>
                    <dd>{displayValue(selectedJob.last_fired_at_display)}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.lastCompleted', 'Last completed')}</dt>
                    <dd>
                      {displayValue(selectedJob.last_completed_at_display)}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.lastRun', 'Last Run')}</dt>
                    <dd>{displayValue(selectedJob.last_run_id)}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.failures', 'Failures')}</dt>
                    <dd>{selectedJob.consecutive_failures}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.remainingRuns', 'Remaining Runs')}</dt>
                    <dd>{remainingRunsLabel(selectedJob)}</dd>
                  </div>
                  <div>
                    <dt>{t('cron.detail.scheduleId', 'Schedule ID')}</dt>
                    <dd>{selectedJob.id}</dd>
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
                      value={formValues.name}
                      placeholder={t(
                        'cron.form.namePlaceholder',
                        'Optional — derived from the prompt',
                      )}
                      disabled={submittingForm}
                      onInput={(value) => updateFormField('name', value)}
                    />
                  </FormField>

                  <FormField
                    controlId="cron-form-agent"
                    label={t('cron.form.agent', 'Agent')}
                    required
                  >
                    <Dropdown
                      id="cron-form-agent"
                      value={formValues.agent_id}
                      options={agentOptions}
                      placeholder={t(
                        'cron.form.agentPlaceholder',
                        'Select an agent',
                      )}
                      ariaLabel={t('cron.form.agent', 'Agent')}
                      disabled={!hasAgents || submittingForm}
                      triggerClass="cron-dropdown"
                      listClass="cron-dropdown-list"
                      onValueChange={(value) =>
                        updateFormField('agent_id', value)}
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
                      value={formValues.prompt}
                      rows={8}
                      placeholder={t(
                        'cron.form.promptPlaceholder',
                        'Describe the run to schedule…',
                      )}
                      disabled={submittingForm}
                      onInput={(value) => updateFormField('prompt', value)}
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
                            checked={isCronSchedule}
                            disabled={submittingForm}
                            onchange={() =>
                              setScheduleType(CRON_SCHEDULE_TYPE_CRON)}
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
                            checked={isIntervalSchedule}
                            disabled={submittingForm}
                            onchange={() =>
                              setScheduleType(CRON_SCHEDULE_TYPE_INTERVAL)}
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
                            checked={isOnceSchedule}
                            disabled={submittingForm}
                            onchange={() =>
                              setScheduleType(CRON_SCHEDULE_TYPE_ONCE)}
                          />
                          <span>{t('cron.form.scheduleType.once', 'Once')}</span
                          >
                        </label>
                      </div>
                    </fieldset>

                    {#if isCronSchedule}
                      <FormField
                        controlId="cron-job-preset"
                        label={t('cron.form.preset', 'Schedule preset')}
                      >
                        <Dropdown
                          id="cron-job-preset"
                          value={selectedPreset}
                          options={presetOptions}
                          ariaLabel={t('cron.form.preset', 'Schedule preset')}
                          disabled={submittingForm}
                          triggerClass="cron-dropdown"
                          listClass="cron-dropdown-list"
                          onValueChange={applyPreset}
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
                          value={formValues.cron_expression}
                          placeholder={t(
                            'cron.form.cronExpressionPlaceholder',
                            '0 9 * * 1-5',
                          )}
                          disabled={submittingForm}
                          onInput={(next) => updateCronExpression(next)}
                        />
                        {#if cronExpressionPreview}
                          <span class="cron-expression-preview">
                            {cronExpressionPreview}
                          </span>
                        {/if}
                      </FormField>
                    {:else if isIntervalSchedule}
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
                          value={formValues.interval_minutes}
                          placeholder={t(
                            'cron.form.intervalMinutesPlaceholder',
                            '120',
                          )}
                          disabled={submittingForm}
                          onInput={(next) =>
                            updateFormField('interval_minutes', next)}
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
                          value={formValues.run_at}
                          disabled={submittingForm}
                          onInput={(next) => updateFormField('run_at', next)}
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
                        value={formValues.repeat}
                        placeholder={isOnceSchedule
                          ? '1'
                          : t('cron.form.repeatPlaceholder', 'Unlimited')}
                        disabled={submittingForm}
                        onInput={(next) => updateFormField('repeat', next)}
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
                        value={formValues.session_id}
                        placeholder={t(
                          'cron.form.sessionIdPlaceholder',
                          'Optional',
                        )}
                        disabled={submittingForm}
                        onInput={(next) => updateFormField('session_id', next)}
                      />
                    </FormField>

                    {#if !isCreating && selectedJob}
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
                          <code>{selectedJob.id}</code>
                          <Button
                            variant="danger"
                            ariaLabel={t(
                              'cron.actions.deleteJob',
                              'Delete job {id}',
                              { id: selectedJob.id },
                            )}
                            data-testid={`cron-delete-${selectedJob.id}`}
                            disabled={submittingForm ||
                              mutatingJobId === selectedJob.id}
                            onClick={() => deleteJob(selectedJob)}
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

            {#if formErrorMessage}
              <div class="cron-form-error">
                <Banner variant="error" role="alert">
                  {formErrorMessage}
                </Banner>
              </div>
            {/if}

            <div class="cron-detail-footer">
              {#if isCreating}
                <Button
                  variant="secondary"
                  disabled={submittingForm}
                  onClick={cancelCreate}
                >
                  {t('common.cancel', 'Cancel')}
                </Button>
              {/if}
              <Button
                variant="primary"
                type="submit"
                disabled={submittingForm || (!isCreating && !isDirty)}
              >
                {submittingForm
                  ? t('common.saving', 'Saving…')
                  : t('common.save', 'Save')}
              </Button>
            </div>
          </form>
        </div>
      {/key}
    {/if}
  </div>

  {#if deleteConfirmJob}
    <ConfirmDialog
      title={t('cron.deleteConfirmTitle', 'Delete Scheduled Run')}
      body={t(
        'cron.deleteConfirm',
        'Delete this job permanently? It will no longer run.',
      )}
      confirmLabel={t('common.delete', 'Delete')}
      onConfirm={confirmDeleteJob}
      onCancel={cancelDeleteJob}
    />
  {/if}

  {#if showDiscardConfirm}
    <ConfirmDialog
      title={t('cron.discardConfirmTitle', 'Discard unsaved changes?')}
      body={t(
        'cron.discardConfirm',
        'Your edits have not been saved. Discard them and continue?',
      )}
      confirmLabel={t('common.discard', 'Discard')}
      onConfirm={confirmDiscard}
      onCancel={cancelDiscard}
    />
  {/if}
</section>
