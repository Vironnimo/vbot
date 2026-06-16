<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import Button from './ui/Button.svelte';
  import {
    createCronJob,
    deleteCronJob,
    disableCronJob,
    enableCronJob,
    listCronJobs,
    rpc,
    updateCronJob,
  } from '$lib/api.js';
  import {
    applyAgentListResponse,
    applyCronListResponse,
    buildCreateCronPayload,
    buildUpdateCronPayload,
    createCronFormValues,
    createCronViewState,
    CRON_SCHEDULE_TYPE_CRON,
    CRON_SCHEDULE_TYPE_ONCE,
    CRON_STATUS_ACTIVE,
    describeCronExpression,
    visibleCronJobs,
  } from '$lib/cronView.js';
  import { t } from '$lib/i18n.js';

  const FORM_MODE_CREATE = 'create';
  const FORM_MODE_EDIT = 'edit';

  let viewState = $state(createCronViewState());
  let formValues = $state(createCronFormValues());
  let cronExpressionPreview = $derived(
    describeCronExpression(formValues.cron_expression),
  );
  let formMode = $state(FORM_MODE_CREATE);
  let isModalOpen = $state(false);
  let formErrorMessage = $state('');
  let submittingForm = $state(false);
  let mutatingJobId = $state('');

  let destroyed = false;
  let jobsRequestId = 0;
  let agentsRequestId = 0;

  let hasAgents = $derived(viewState.agents.length > 0);
  let isLoading = $derived(viewState.loadingAgents || viewState.loadingJobs);
  let jobs = $derived(visibleCronJobs(viewState.jobs));
  let isCronSchedule = $derived(
    formValues.schedule_type === CRON_SCHEDULE_TYPE_CRON,
  );
  let modalTitle = $derived(
    formMode === FORM_MODE_CREATE
      ? t('cron.modal.createTitle', 'Create cron job')
      : t('cron.modal.editTitle', 'Edit cron job'),
  );
  let agentOptions = $derived(
    viewState.agents.map((agent) => ({
      value: agent.id,
      label: agent.name,
      secondaryLabel: agent.id,
    })),
  );
  let agentNameById = $derived(
    new Map(viewState.agents.map((agent) => [agent.id, agent.name])),
  );

  onMount(() => {
    loadInitialData();

    return () => {
      destroyed = true;
      isModalOpen = false;
      formErrorMessage = '';
    };
  });

  async function loadInitialData() {
    viewState.errorMessage = '';
    await Promise.all([loadAgents(), loadJobs()]);
  }

  async function loadAgents() {
    const requestId = agentsRequestId + 1;
    agentsRequestId = requestId;
    viewState.loadingAgents = true;

    try {
      const result = await rpc('agent.list');
      if (destroyed || requestId !== agentsRequestId) {
        return;
      }

      applyAgentListResponse(viewState, result);
      if (!formValues.agent_id && viewState.agents.length > 0) {
        formValues.agent_id = viewState.agents[0].id;
      }
    } catch (error) {
      if (destroyed || requestId !== agentsRequestId) {
        return;
      }

      viewState.errorMessage = `${t('cron.errors.loadAgents', 'Agents could not be loaded for cron jobs.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
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

    try {
      const result = await listCronJobs();
      if (destroyed || requestId !== jobsRequestId) {
        return;
      }

      applyCronListResponse(viewState, result);
    } catch (error) {
      if (destroyed || requestId !== jobsRequestId) {
        return;
      }

      viewState.errorMessage = `${t('cron.errors.loadJobs', 'Cron jobs could not be loaded.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      if (!destroyed && requestId === jobsRequestId) {
        viewState.loadingJobs = false;
      }
    }
  }

  function openCreateModal() {
    formMode = FORM_MODE_CREATE;
    formValues = createCronFormValues();
    formValues.agent_id = viewState.agents[0]?.id ?? '';
    formErrorMessage = '';
    isModalOpen = true;
  }

  function openEditModal(job) {
    formMode = FORM_MODE_EDIT;
    formValues = createCronFormValues(job);
    if (!formValues.agent_id) {
      formValues.agent_id = viewState.agents[0]?.id ?? '';
    }
    formErrorMessage = '';
    isModalOpen = true;
  }

  function closeModal() {
    if (submittingForm) {
      return;
    }

    isModalOpen = false;
    formErrorMessage = '';
  }

  function handleDocumentKeydown(event) {
    if (event.key === 'Escape' && isModalOpen && !submittingForm) {
      closeModal();
    }
  }

  function handleOverlayClick(event) {
    if (event.target === event.currentTarget) {
      closeModal();
    }
  }

  function setScheduleType(scheduleType) {
    formValues.schedule_type = scheduleType;
    formErrorMessage = '';
  }

  function updateFormField(fieldName, value) {
    formValues[fieldName] = value;
    formErrorMessage = '';
  }

  function validateFormValues() {
    const hasCoreValues =
      formValues.agent_id.trim().length > 0 &&
      formValues.prompt.trim().length > 0;
    const hasScheduleValue =
      formValues.schedule_type === CRON_SCHEDULE_TYPE_CRON
        ? formValues.cron_expression.trim().length > 0
        : formValues.run_at.trim().length > 0;

    if (!hasCoreValues || !hasScheduleValue) {
      formErrorMessage = t(
        'cron.errors.missingRequired',
        'Agent, prompt, and schedule details are required.',
      );
      return false;
    }

    return true;
  }

  async function submitForm(event) {
    event.preventDefault();

    if (!validateFormValues()) {
      return;
    }

    submittingForm = true;
    formErrorMessage = '';
    viewState.errorMessage = '';
    viewState.statusMessage = '';

    try {
      if (formMode === FORM_MODE_CREATE) {
        await createCronJob(buildCreateCronPayload(formValues));
        viewState.statusMessage = t(
          'cron.messages.created',
          'Cron job created.',
        );
      } else {
        await updateCronJob(buildUpdateCronPayload(formValues));
        viewState.statusMessage = t(
          'cron.messages.updated',
          'Cron job updated.',
        );
      }

      isModalOpen = false;
      await loadJobs({ silent: true });
    } catch (error) {
      formErrorMessage = `${t('cron.errors.save', 'Cron job could not be saved.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      submittingForm = false;
    }
  }

  async function toggleJob(job) {
    if (!job?.id) {
      return;
    }

    mutatingJobId = job.id;
    viewState.errorMessage = '';
    viewState.statusMessage = '';

    try {
      if (job.status === CRON_STATUS_ACTIVE) {
        await disableCronJob(job.id);
        viewState.statusMessage = t(
          'cron.messages.disabled',
          'Cron job disabled.',
        );
      } else {
        await enableCronJob(job.id);
        viewState.statusMessage = t(
          'cron.messages.enabled',
          'Cron job enabled.',
        );
      }

      await loadJobs({ silent: true });
    } catch (error) {
      viewState.errorMessage = `${t('cron.errors.toggle', 'Cron job status could not be updated.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      mutatingJobId = '';
    }
  }

  async function deleteJob(job) {
    if (!job?.id) {
      return;
    }

    const confirmDelete =
      typeof globalThis.confirm === 'function'
        ? globalThis.confirm(
            t('cron.deleteConfirm', 'Delete cron job for agent {agentId}?', {
              agentId: job.agent_id,
            }),
          )
        : true;

    if (!confirmDelete) {
      return;
    }

    mutatingJobId = job.id;
    viewState.errorMessage = '';
    viewState.statusMessage = '';

    try {
      await deleteCronJob(job.id);
      viewState.statusMessage = t('cron.messages.deleted', 'Cron job deleted.');
      await loadJobs({ silent: true });
    } catch (error) {
      viewState.errorMessage = `${t('cron.errors.delete', 'Cron job could not be deleted.')} ${errorMessageText(error, t('common.unknown', 'Unknown'))}`;
    } finally {
      mutatingJobId = '';
    }
  }

  function agentLabel(agentId) {
    return (
      agentNameById.get(agentId) || agentId || t('common.unknown', 'Unknown')
    );
  }

  function displayValue(value) {
    return value || t('cron.notAvailable', '—');
  }

  function timezoneLabel(job) {
    return job.timezone || t('cron.systemDefault', 'System default');
  }

  function statusLabel(status) {
    if (status === CRON_STATUS_ACTIVE) {
      return t('cron.status.active', 'Active');
    }

    if (status === 'paused') {
      return t('cron.status.paused', 'Paused');
    }

    return t('cron.status.completed', 'Completed');
  }

  function statusChipClass(status) {
    if (status === CRON_STATUS_ACTIVE) {
      return 'chip-green';
    }

    if (status === 'paused') {
      return 'chip-amber';
    }

    return 'chip-red';
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

<svelte:document onkeydown={handleDocumentKeydown} />

<section class="cron-view view active" aria-labelledby="cron-title">
  <header class="cron-view__header">
    <div>
      <p class="cron-view__eyebrow">
        {t('cron.eyebrow', 'Scheduled automation')}
      </p>
      <h2 id="cron-title" class="cron-view__title">
        {t('cron.title', 'Cron')}
      </h2>
      <p class="cron-view__subtitle">
        {t(
          'cron.subtitle',
          'Manage scheduled agent runs. Completed jobs are hidden from this list.',
        )}
      </p>
    </div>

    <div class="cron-view__header-actions">
      <Button variant="secondary" onClick={() => loadJobs()}>
        {t('common.refresh', 'Refresh')}
      </Button>
      <Button variant="primary" disabled={!hasAgents} onClick={openCreateModal}>
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {t('cron.newJob', 'New job')}
      </Button>
    </div>
  </header>

  {#if !hasAgents}
    <p class="cron-view__notice cron-view__notice--warn" role="status">
      {t('cron.noAgents', 'Create an agent before adding cron jobs.')}
    </p>
  {/if}

  {#if viewState.errorMessage}
    <p class="cron-view__notice cron-view__notice--error" role="alert">
      {viewState.errorMessage}
    </p>
  {/if}

  {#if viewState.statusMessage}
    <p class="cron-view__notice" role="status">{viewState.statusMessage}</p>
  {/if}

  {#if isLoading}
    <div class="cron-view__state">
      <p class="cron-view__state-title">
        {t('cron.loading', 'Loading cron jobs…')}
      </p>
    </div>
  {:else if jobs.length === 0}
    <div class="cron-view__state">
      <p class="cron-view__state-title">
        {t('cron.emptyTitle', 'No scheduled jobs')}
      </p>
      <p class="cron-view__state-subtitle">
        {t(
          'cron.emptySubtitle',
          'Create a job to run an agent prompt on a schedule.',
        )}
      </p>
    </div>
  {:else}
    <div class="cron-view__table-wrap">
      <table
        class="cron-view__table"
        aria-label={t('cron.table.caption', 'Cron jobs')}
      >
        <thead>
          <tr>
            <th>{t('cron.table.agent', 'Agent')}</th>
            <th>{t('cron.table.prompt', 'Prompt')}</th>
            <th>{t('cron.table.schedule', 'Schedule')}</th>
            <th>{t('cron.table.timezone', 'Timezone')}</th>
            <th>{t('cron.table.status', 'Status')}</th>
            <th>{t('cron.table.lastFired', 'Last fired')}</th>
            <th>{t('cron.table.nextFire', 'Next fire')}</th>
            <th>{t('cron.table.actions', 'Actions')}</th>
          </tr>
        </thead>
        <tbody>
          {#each jobs as job (job.id)}
            <tr>
              <td class="cron-view__mono">{agentLabel(job.agent_id)}</td>
              <td class="cron-view__prompt" title={job.prompt}>{job.prompt}</td>
              <td
                class="cron-view__mono"
                title={describeCronExpression(job.cron_expression)}
              >
                {displayValue(job.schedule_description)}
              </td>
              <td class="cron-view__mono">{timezoneLabel(job)}</td>
              <td>
                <span class={`chip ${statusChipClass(job.status)}`}>
                  {statusLabel(job.status)}
                </span>
              </td>
              <td class="cron-view__mono">
                {displayValue(job.last_fired_at_display)}
              </td>
              <td class="cron-view__mono">
                {displayValue(job.next_fire_at_display)}
              </td>
              <td>
                <div class="cron-view__actions">
                  <button
                    type="button"
                    class="toggle"
                    class:on={job.status === CRON_STATUS_ACTIVE}
                    role="switch"
                    aria-checked={job.status === CRON_STATUS_ACTIVE}
                    aria-label={job.status === CRON_STATUS_ACTIVE
                      ? t('cron.actions.disableJob', 'Disable job {id}', {
                          id: job.id,
                        })
                      : t('cron.actions.enableJob', 'Enable job {id}', {
                          id: job.id,
                        })}
                    disabled={submittingForm || mutatingJobId === job.id}
                    data-testid={`cron-toggle-${job.id}`}
                    onclick={() => toggleJob(job)}
                  >
                    <span class="t-knob"></span>
                  </button>
                  <Button
                    variant="secondary"
                    ariaLabel={t('cron.actions.editJob', 'Edit job {id}', {
                      id: job.id,
                    })}
                    data-testid={`cron-edit-${job.id}`}
                    disabled={submittingForm || mutatingJobId === job.id}
                    onClick={() => openEditModal(job)}
                  >
                    {t('common.edit', 'Edit')}
                  </Button>
                  <Button
                    variant="danger"
                    ariaLabel={t('cron.actions.deleteJob', 'Delete job {id}', {
                      id: job.id,
                    })}
                    data-testid={`cron-delete-${job.id}`}
                    disabled={submittingForm || mutatingJobId === job.id}
                    onClick={() => deleteJob(job)}
                  >
                    {t('common.delete', 'Delete')}
                  </Button>
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if isModalOpen}
    <div
      class="modal-overlay open"
      role="presentation"
      onclick={handleOverlayClick}
    >
      <div
        class="modal cron-view__modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cron-modal-title"
      >
        <div class="modal-header">
          <h3 id="cron-modal-title" class="modal-title">{modalTitle}</h3>
          <button
            type="button"
            class="modal-close"
            aria-label={t('common.close', 'Close')}
            onclick={closeModal}
          >
            ×
          </button>
        </div>

        <form onsubmit={submitForm}>
          <div class="modal-body">
            <label class="modal-field">
              <span class="modal-label">{t('cron.form.agent', 'Agent')}</span>
              <Dropdown
                id="cron-form-agent"
                value={formValues.agent_id}
                options={agentOptions}
                placeholder={t('cron.form.agentPlaceholder', 'Select an agent')}
                ariaLabel={t('cron.form.agent', 'Agent')}
                disabled={!hasAgents || submittingForm}
                triggerClass="cron-view__dropdown"
                listClass="cron-view__dropdown-list"
                onValueChange={(value) => updateFormField('agent_id', value)}
              />
            </label>

            <label class="modal-field">
              <span class="modal-label">{t('cron.form.prompt', 'Prompt')}</span>
              <textarea
                id="cron-job-prompt"
                class="s-input cron-view__textarea"
                value={formValues.prompt}
                placeholder={t(
                  'cron.form.promptPlaceholder',
                  'Describe the run to schedule…',
                )}
                disabled={submittingForm}
                oninput={(event) =>
                  updateFormField('prompt', event.currentTarget.value)}
              ></textarea>
            </label>

            <fieldset class="modal-field cron-view__radio-fieldset">
              <legend class="modal-label">
                {t('cron.form.scheduleType', 'Schedule type')}
              </legend>
              <div class="cron-view__radio-group">
                <label class="cron-view__radio-option">
                  <input
                    type="radio"
                    name="cron-schedule-type"
                    value={CRON_SCHEDULE_TYPE_CRON}
                    checked={isCronSchedule}
                    disabled={submittingForm}
                    onchange={() => setScheduleType(CRON_SCHEDULE_TYPE_CRON)}
                  />
                  <span>{t('cron.form.scheduleType.cron', 'Cron')}</span>
                </label>
                <label class="cron-view__radio-option">
                  <input
                    type="radio"
                    name="cron-schedule-type"
                    value={CRON_SCHEDULE_TYPE_ONCE}
                    checked={!isCronSchedule}
                    disabled={submittingForm}
                    onchange={() => setScheduleType(CRON_SCHEDULE_TYPE_ONCE)}
                  />
                  <span>{t('cron.form.scheduleType.once', 'Once')}</span>
                </label>
              </div>
            </fieldset>

            {#if isCronSchedule}
              <label class="modal-field">
                <span class="modal-label"
                  >{t('cron.form.cronExpression', 'Cron expression')}</span
                >
                <input
                  id="cron-job-expression"
                  class="s-input"
                  type="text"
                  value={formValues.cron_expression}
                  placeholder={t(
                    'cron.form.cronExpressionPlaceholder',
                    '0 9 * * 1-5',
                  )}
                  disabled={submittingForm}
                  oninput={(event) =>
                    updateFormField(
                      'cron_expression',
                      event.currentTarget.value,
                    )}
                />
                {#if cronExpressionPreview}
                  <span class="cron-view__expression-preview">
                    {cronExpressionPreview}
                  </span>
                {/if}
              </label>
            {:else}
              <label class="modal-field">
                <span class="modal-label">{t('cron.form.runAt', 'Run at')}</span
                >
                <input
                  id="cron-job-run-at"
                  class="s-input"
                  type="datetime-local"
                  value={formValues.run_at}
                  disabled={submittingForm}
                  oninput={(event) =>
                    updateFormField('run_at', event.currentTarget.value)}
                />
              </label>
            {/if}

            <label class="modal-field">
              <span class="modal-label"
                >{t('cron.form.timezone', 'Timezone')}</span
              >
              <input
                id="cron-job-timezone"
                class="s-input"
                type="text"
                value={formValues.timezone}
                placeholder={t(
                  'cron.form.timezonePlaceholder',
                  'System default',
                )}
                disabled={submittingForm}
                oninput={(event) =>
                  updateFormField('timezone', event.currentTarget.value)}
              />
            </label>

            <label class="modal-field">
              <span class="modal-label"
                >{t('cron.form.sessionId', 'Session ID')}</span
              >
              <input
                id="cron-job-session"
                class="s-input"
                type="text"
                value={formValues.session_id}
                placeholder={t('cron.form.sessionIdPlaceholder', 'Optional')}
                disabled={submittingForm}
                oninput={(event) =>
                  updateFormField('session_id', event.currentTarget.value)}
              />
            </label>

            {#if formErrorMessage}
              <p
                class="cron-view__notice cron-view__notice--error"
                role="alert"
              >
                {formErrorMessage}
              </p>
            {/if}
          </div>

          <div class="modal-footer">
            <Button
              variant="secondary"
              disabled={submittingForm}
              onClick={closeModal}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button variant="primary" type="submit" disabled={submittingForm}>
              {submittingForm
                ? t('common.saving', 'Saving…')
                : t('common.save', 'Save')}
            </Button>
          </div>
        </form>
      </div>
    </div>
  {/if}
</section>

<style>
  .cron-view__expression-preview {
    margin-top: 4px;
    color: var(--text-med);
    font-size: 12px;
    line-height: 1.4;
  }

  .cron-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 14px;
    overflow: hidden;
    padding: 24px 28px 28px;
    background: var(--bg);
  }

  .cron-view__header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .cron-view__eyebrow {
    margin: 0 0 6px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .cron-view__title {
    margin: 0;
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .cron-view__subtitle {
    max-width: 760px;
    margin: 6px 0 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .cron-view__header-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
  }

  .cron-view__notice {
    margin: 0;
    padding: 11px 14px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--green);
    border-radius: var(--r-md);
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.4;
    background: var(--surface);
  }

  .cron-view__notice--error {
    border-left-color: var(--red);
    color: var(--red);
  }

  .cron-view__notice--warn {
    border-left-color: var(--amber);
    color: var(--amber);
  }

  .cron-view__state {
    display: flex;
    min-height: 0;
    flex: 1;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 8px;
    padding: 28px;
    border: 1px dashed var(--border);
    border-radius: var(--r-lg);
    background: rgba(255, 255, 255, 0.02);
    text-align: center;
  }

  .cron-view__state-title {
    margin: 0;
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
  }

  .cron-view__state-subtitle {
    max-width: 560px;
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.5;
  }

  .cron-view__table-wrap {
    min-height: 0;
    flex: 1;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .cron-view__table {
    width: 100%;
    min-width: 1020px;
    border-collapse: separate;
    border-spacing: 0;
  }

  .cron-view__table th,
  .cron-view__table td {
    padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    text-align: left;
    vertical-align: middle;
  }

  .cron-view__table th {
    position: sticky;
    top: 0;
    z-index: 1;
    color: var(--text-lo);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .cron-view__table tbody tr:hover {
    background: rgba(232, 135, 10, 0.05);
  }

  .cron-view__table tbody tr:last-child td {
    border-bottom: 0;
  }

  .cron-view__mono,
  .cron-view__prompt {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .cron-view__prompt {
    display: inline-block;
    max-width: 300px;
    overflow: hidden;
    color: var(--text-hi);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cron-view__actions {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .cron-view__actions :global(.btn-secondary),
  .cron-view__actions :global(.btn-danger) {
    padding: 4px 10px;
    font-size: 11.5px;
  }

  .cron-view__modal {
    width: 560px;
    max-width: calc(100vw - 40px);
  }

  .cron-view__textarea {
    min-height: 92px;
    resize: vertical;
  }

  .cron-view__radio-fieldset {
    border: 0;
    padding: 0;
    margin: 0;
  }

  .cron-view__radio-group {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    padding: 10px 12px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .cron-view__radio-option {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    color: var(--text-med);
    font-size: 12.5px;
  }

  .cron-view__radio-option input {
    accent-color: var(--accent);
  }

  :global(.cron-view__dropdown),
  :global(.cron-view__dropdown-list) {
    width: 100%;
    min-width: 0;
  }

  :global(.cron-view__dropdown .dropdown-primitive__trigger),
  :global(.cron-view__dropdown .dropdown-primitive__option) {
    font-family: var(--font-mono);
    font-size: 12.5px;
  }

  :global(.cron-view__dropdown-list) {
    max-height: 220px;
    overflow-y: auto;
  }

  @media (max-width: 960px) {
    .cron-view {
      padding: 20px;
    }

    .cron-view__header,
    .cron-view__header-actions {
      align-items: stretch;
      flex-direction: column;
    }

    .cron-view__header-actions {
      justify-content: flex-start;
    }

    .cron-view__table {
      min-width: 900px;
    }
  }
</style>
