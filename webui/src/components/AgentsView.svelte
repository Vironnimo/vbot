<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import {
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';

  let {
    sharedSelectedAgentId = '',
    onAgentsChanged,
    onAgentSelected,
  } = $props();

  let agents = $state([]);
  let selectedAgentId = $state('');
  let formMode = $state(AGENT_FORM_MODE_CREATE);
  let formValues = $state(createAgentFormValues());
  let formErrors = $state({});
  let isLoading = $state(false);
  let isSaving = $state(false);
  let isDeleting = $state(false);
  let errorMessage = $state('');
  let statusMessage = $state('');

  let selectedAgent = $derived(
    agents.find((agent) => agent.id === selectedAgentId) ?? null,
  );
  let canDeleteSelectedAgent = $derived(
    Boolean(selectedAgent) && agents.length > 1,
  );
  let submitLabel = $derived(
    formMode === AGENT_FORM_MODE_CREATE
      ? t('agents.form.submitCreate', 'Create agent')
      : t('agents.form.submitUpdate', 'Save changes'),
  );
  let formTitle = $derived(
    formMode === AGENT_FORM_MODE_CREATE
      ? t('agents.create', 'Create Agent')
      : t('agents.edit', 'Edit Agent'),
  );

  $effect(() => {
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== selectedAgentId &&
      agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      selectAgent(sharedSelectedAgentId);
    }
  });

  onMount(() => {
    loadAgents({ preferredAgentId: sharedSelectedAgentId });
  });

  async function loadAgents(options = {}) {
    isLoading = true;
    errorMessage = '';

    try {
      const result = await rpc('agent.list');
      agents = Array.isArray(result?.agents) ? result.agents : [];
      const preferredAgentId = options.preferredAgentId ?? selectedAgentId;
      selectAgent(resolveSelectedAgentId(agents, preferredAgentId), {
        clearNotices: false,
      });
      notifyAgentsChanged();
    } catch (error) {
      errorMessage = viewErrorMessage(error, t('agents.loadError'));
    } finally {
      isLoading = false;
    }
  }

  function resolveSelectedAgentId(nextAgents, preferredAgentId) {
    if (nextAgents.some((agent) => agent.id === preferredAgentId)) {
      return preferredAgentId;
    }

    return nextAgents[0]?.id ?? '';
  }

  function selectAgent(agentId, options = {}) {
    const shouldClearNotices = options.clearNotices ?? true;
    selectedAgentId = agentId;
    const agent = agents.find((item) => item.id === agentId) ?? null;

    if (agent) {
      formMode = AGENT_FORM_MODE_EDIT;
      formValues = createAgentFormValues(agent);
      onAgentSelected?.(agent);
    } else {
      startCreate();
    }

    formErrors = {};
    if (shouldClearNotices) {
      clearNotices();
    }
  }

  function startCreate() {
    selectedAgentId = '';
    formMode = AGENT_FORM_MODE_CREATE;
    formValues = createAgentFormValues();
    formErrors = {};
    clearNotices();
  }

  function clearNotices() {
    errorMessage = '';
    statusMessage = '';
  }

  async function saveAgent(event) {
    event.preventDefault();
    const result = normalizeAgentForm(formValues, { mode: formMode });
    formErrors = result.errors;
    statusMessage = '';
    errorMessage = '';

    if (!result.isValid) {
      errorMessage = t(
        'errors.validation',
        'Check the highlighted fields and try again.',
      );
      return;
    }

    isSaving = true;
    try {
      const method =
        formMode === AGENT_FORM_MODE_CREATE ? 'agent.create' : 'agent.update';
      const savedAgent = await rpc(method, result.payload);
      statusMessage =
        formMode === AGENT_FORM_MODE_CREATE
          ? t('agents.created', 'Agent created.')
          : t('agents.updated', 'Agent updated.');
      await loadAgents({
        preferredAgentId: savedAgent.id ?? result.payload.id,
      });
    } catch (error) {
      errorMessage = viewErrorMessage(error, t('agents.saveError'));
    } finally {
      isSaving = false;
    }
  }

  async function deleteSelectedAgent() {
    if (!selectedAgent) {
      return;
    }

    if (!canDeleteSelectedAgent) {
      errorMessage = t(
        'errors.minimumAgents',
        'At least one agent must remain.',
      );
      return;
    }

    isDeleting = true;
    statusMessage = '';
    errorMessage = '';

    try {
      await rpc('agent.delete', { id: selectedAgent.id });
      statusMessage = t('agents.deleted', 'Agent deleted.');
      await loadAgents();
    } catch (error) {
      errorMessage = viewErrorMessage(error, t('agents.deleteError'));
    } finally {
      isDeleting = false;
    }
  }

  function notifyAgentsChanged() {
    onAgentsChanged?.(agents);
  }

  function fieldError(fieldName) {
    if (!formErrors[fieldName]) {
      return '';
    }

    if (formErrors[fieldName] === 'required') {
      return t('agents.form.required', 'This field is required.');
    }

    return t(
      'errors.validation',
      'Check the highlighted fields and try again.',
    );
  }

  function viewErrorMessage(error, fallback) {
    if (error?.code === 'last_agent') {
      return t('errors.minimumAgents', 'At least one agent must remain.');
    }

    return (
      error?.message ||
      fallback ||
      t('errors.generic', 'Something went wrong. Try again.')
    );
  }
</script>

<section class="agents-view" aria-labelledby="agents-view-title">
  <aside class="agents-view__list-pane" aria-labelledby="agents-list-title">
    <div class="agents-view__pane-header">
      <div>
        <p class="agents-view__pane-title" id="agents-list-title">
          {t('agents.title', 'Agents')}
        </p>
        <p class="agents-view__pane-subtitle">
          {t('agents.listTitle', 'Available agents')}
        </p>
      </div>
      <button
        class="btn-new agents-view__new-button"
        type="button"
        onclick={startCreate}
      >
        <svg aria-hidden="true" viewBox="0 0 14 14">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {t('common.new', 'New')}
      </button>
    </div>

    <div class="agents-view__list-scroll">
      {#if isLoading}
        <p class="agents-view__empty-list">
          {t('agents.loading', 'Loading agents…')}
        </p>
      {:else if agents.length === 0}
        <p class="agents-view__empty-list">
          {t('agents.empty', 'No agents found.')}
        </p>
      {:else}
        <div class="agents-view__list">
          {#each agents as agent (agent.id)}
            <button
              class:agents-view__agent-row--active={agent.id ===
                selectedAgentId}
              class="agents-view__agent-row"
              type="button"
              onclick={() => selectAgent(agent.id)}
            >
              <span class="agents-view__agent-bar" aria-hidden="true"></span>
              <span class="agents-view__agent-row-inner">
                <span class="agents-view__agent-name"
                  >{agent.name || agent.id}</span
                >
                <span class="agents-view__agent-sub">
                  {agent.model || t('agents.noModel', 'No model')}
                </span>
              </span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  </aside>

  <form class="agents-view__detail-pane" onsubmit={saveAgent}>
    <div class="agents-view__detail-top">
      <div>
        <p class="agents-view__status">{t('app.ready', 'Ready')}</p>
        <h2 id="agents-view-title" class="agents-view__heading">
          {formMode === AGENT_FORM_MODE_CREATE
            ? t('agents.create', 'Create Agent')
            : selectedAgent?.name || formTitle}
        </h2>
        <p class="agents-view__detail-sub">
          {formMode === AGENT_FORM_MODE_CREATE
            ? t(
                'agents.createDescription',
                'Define a file-backed agent configuration for chat.',
              )
            : `${t('agents.idLabel', 'id')}: ${selectedAgent?.id ?? ''}`}
        </p>
      </div>
      <div class="agents-view__detail-buttons">
        <button class="btn-outline" type="button" onclick={loadAgents}>
          {t('common.refresh', 'Refresh')}
        </button>
        {#if formMode === AGENT_FORM_MODE_EDIT}
          <button class="btn-outline" type="button" onclick={startCreate}>
            {t('agents.create', 'Create Agent')}
          </button>
        {/if}
      </div>
    </div>

    {#if errorMessage}
      <p class="agents-view__notice agents-view__notice--error" role="alert">
        {errorMessage}
      </p>
    {/if}

    {#if statusMessage}
      <p class="agents-view__notice" role="status">{statusMessage}</p>
    {/if}

    <div class="agents-view__detail-group">
      <div class="agents-view__group-title">
        {t('agents.group.identity', 'Identity')}
      </div>
      <div class="agents-view__fields">
        <label class="agents-view__field">
          <span>{t('agents.form.id', 'Agent ID')}</span>
          <input
            class:agents-view__invalid={formErrors.id}
            type="text"
            bind:value={formValues.id}
            disabled={formMode === AGENT_FORM_MODE_EDIT}
            placeholder={t('agents.form.idPlaceholder', 'main-agent')}
            aria-invalid={Boolean(formErrors.id)}
            aria-describedby="agent-id-help agent-id-error"
          />
          <small id="agent-id-help">
            {t('agents.form.idHelp', 'Agent IDs are immutable after creation.')}
          </small>
          {#if formErrors.id}
            <small id="agent-id-error" class="agents-view__field-error">
              {fieldError('id')}
            </small>
          {/if}
        </label>

        <label class="agents-view__field">
          <span>{t('agents.form.name', 'Name')}</span>
          <input
            class:agents-view__invalid={formErrors.name}
            type="text"
            bind:value={formValues.name}
            placeholder={t('agents.form.namePlaceholder', 'Main Agent')}
            aria-invalid={Boolean(formErrors.name)}
          />
          {#if formErrors.name}
            <small class="agents-view__field-error">{fieldError('name')}</small>
          {/if}
        </label>
      </div>
    </div>

    <div class="agents-view__detail-group">
      <div class="agents-view__group-title">
        {t('agents.group.model', 'Model')}
      </div>
      <div class="agents-view__fields">
        <label class="agents-view__field agents-view__field--wide">
          <span>{t('agents.form.model', 'Model')}</span>
          <input
            type="text"
            bind:value={formValues.model}
            placeholder={t('agents.form.modelPlaceholder', 'provider/model-id')}
          />
        </label>

        <label class="agents-view__field">
          <span>{t('agents.form.fallbackModel', 'Fallback model')}</span>
          <input
            type="text"
            bind:value={formValues.fallback_model}
            placeholder={t('common.optional', 'Optional')}
          />
        </label>

        <label class="agents-view__field">
          <span>{t('agents.form.thinkingEffort', 'Thinking effort')}</span>
          <input
            type="text"
            bind:value={formValues.thinking_effort}
            placeholder={t('agents.form.thinkingPlaceholder', 'medium')}
          />
        </label>

        <label class="agents-view__field">
          <span>{t('agents.form.temperature', 'Temperature')}</span>
          <input
            class:agents-view__invalid={formErrors.temperature}
            type="number"
            step="0.01"
            bind:value={formValues.temperature}
            aria-invalid={Boolean(formErrors.temperature)}
          />
          {#if formErrors.temperature}
            <small class="agents-view__field-error">
              {fieldError('temperature')}
            </small>
          {/if}
        </label>
      </div>
    </div>

    <div class="agents-view__detail-group">
      <div class="agents-view__group-title">
        {t('agents.group.access', 'Access')}
      </div>
      <div class="agents-view__fields">
        <label class="agents-view__field agents-view__field--wide">
          <span>{t('agents.form.allowedTools', 'Allowed tools')}</span>
          <textarea rows="5" bind:value={formValues.allowed_tools}></textarea>
          <small>{t('agents.form.listHelp', 'Enter one item per line.')}</small>
        </label>

        <label class="agents-view__field agents-view__field--wide">
          <span>{t('agents.form.allowedSkills', 'Allowed skills')}</span>
          <textarea rows="5" bind:value={formValues.allowed_skills}></textarea>
          <small>{t('agents.form.listHelp', 'Enter one item per line.')}</small>
        </label>
      </div>
    </div>

    <div class="agents-view__detail-group">
      <div class="agents-view__group-title">
        {t('agents.group.workspace', 'Workspace')}
      </div>
      <div class="agents-view__fields">
        <div class="agents-view__readonly-field agents-view__field--wide">
          <span>{t('agents.form.workspace', 'Workspace')}</span>
          {#if formValues.workspace}
            <code>{formValues.workspace}</code>
          {:else}
            <p class="agents-view__muted">
              {t(
                'agents.form.workspaceAssignedByServer',
                'Workspace is assigned by the server when the agent is created.',
              )}
            </p>
          {/if}
          <small>
            {t(
              'agents.form.workspaceReadOnly',
              'Workspace is read-only in this WebUI.',
            )}
          </small>
        </div>
      </div>
    </div>

    <div class="agents-view__actions">
      <button class="btn-new" type="submit" disabled={isSaving}>
        {isSaving ? t('common.saving', 'Saving…') : submitLabel}
      </button>

      {#if formMode === AGENT_FORM_MODE_EDIT}
        <button
          class="btn-outline btn-danger"
          type="button"
          disabled={isDeleting || !canDeleteSelectedAgent}
          title={!canDeleteSelectedAgent
            ? t(
                'agents.deleteDisabledMinimum',
                'The last remaining agent cannot be deleted.',
              )
            : t('agents.delete', 'Delete Agent')}
          onclick={deleteSelectedAgent}
        >
          {isDeleting
            ? t('common.loading', 'Loading…')
            : t('agents.delete', 'Delete Agent')}
        </button>
      {/if}
    </div>

    {#if formMode === AGENT_FORM_MODE_EDIT && !canDeleteSelectedAgent}
      <p class="agents-view__muted">
        {t(
          'agents.deleteDisabledMinimum',
          'The last remaining agent cannot be deleted.',
        )}
      </p>
    {/if}
  </form>
</section>

<style>
  .agents-view {
    display: flex;
    width: 100%;
    height: 100%;
    min-height: 0;
    color: var(--text-hi);
  }

  .agents-view p,
  .agents-view h2 {
    margin: 0;
  }

  .agents-view__list-pane {
    display: flex;
    width: 240px;
    min-width: 240px;
    flex-direction: column;
    overflow: hidden;
    border-right: 1px solid var(--border);
    background: var(--surface);
  }

  .agents-view__pane-header {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: 12px 14px 10px;
    border-bottom: 1px solid var(--border);
  }

  .agents-view__pane-title,
  .agents-view__status,
  .agents-view__group-title,
  .agents-view__field > span,
  .agents-view__readonly-field > span {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.07em;
    line-height: 1;
    text-transform: uppercase;
  }

  .agents-view__pane-subtitle {
    margin-top: 4px;
    color: var(--text-lo);
    font-size: 12px;
  }

  .agents-view__new-button svg {
    width: 11px;
    height: 11px;
  }

  .agents-view__list-scroll {
    flex: 1;
    overflow-y: auto;
    padding: 4px 0;
  }

  .agents-view__empty-list {
    padding: var(--space-md);
    color: var(--text-lo);
    font-size: 12.5px;
  }

  .agents-view__list {
    display: flex;
    flex-direction: column;
  }

  .agents-view__agent-row {
    display: flex;
    align-items: stretch;
    border: 0;
    background: transparent;
    color: var(--text-hi);
    text-align: left;
    transition: background 100ms ease;
  }

  .agents-view__agent-row:hover {
    background: var(--surface-2);
  }

  .agents-view__agent-row--active {
    background: var(--accent-dim);
  }

  .agents-view__agent-bar {
    width: 2px;
    flex-shrink: 0;
    background: transparent;
  }

  .agents-view__agent-row--active .agents-view__agent-bar {
    background: var(--accent);
  }

  .agents-view__agent-row-inner {
    min-width: 0;
    flex: 1;
    padding: 7px 12px 7px 10px;
  }

  .agents-view__agent-name,
  .agents-view__agent-sub {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agents-view__agent-name {
    color: var(--text-hi);
    font-size: 13px;
    font-weight: 500;
  }

  .agents-view__agent-row--active .agents-view__agent-name {
    color: var(--accent);
  }

  .agents-view__agent-sub {
    margin-top: 1px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
  }

  .agents-view__detail-pane {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 22px;
    overflow-y: auto;
    padding: 26px 30px;
  }

  .agents-view__detail-top,
  .agents-view__actions {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .agents-view__detail-buttons,
  .agents-view__actions {
    flex-wrap: wrap;
  }

  .agents-view__detail-buttons {
    display: flex;
    gap: var(--space-sm);
  }

  .agents-view__status {
    margin-bottom: var(--space-sm);
    color: var(--accent);
  }

  .agents-view__heading {
    color: var(--text-hi);
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.03em;
    line-height: 1.2;
  }

  .agents-view__detail-sub,
  .agents-view__muted,
  .agents-view small {
    color: var(--text-lo);
    font-size: 12.5px;
    line-height: 1.4;
  }

  .agents-view__detail-sub {
    margin-top: 4px;
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .agents-view__notice {
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-left: 2px solid var(--green);
    border-radius: var(--r-md);
    background: var(--surface);
    color: var(--text-med);
  }

  .agents-view__notice--error {
    border-left-color: var(--red);
    color: var(--red);
  }

  .agents-view__detail-group {
    flex-shrink: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
  }

  .agents-view__group-title {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .agents-view__fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-md);
    padding: 16px;
  }

  .agents-view__field,
  .agents-view__readonly-field {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: var(--space-xs);
  }

  .agents-view__field--wide {
    grid-column: 1 / -1;
  }

  .agents-view input,
  .agents-view textarea,
  .agents-view__readonly-field code {
    width: 100%;
    padding: 7px 11px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.5;
  }

  .agents-view textarea {
    min-height: 112px;
    resize: vertical;
  }

  .agents-view input:disabled {
    color: var(--text-lo);
  }

  .agents-view__readonly-field code {
    display: block;
    overflow-wrap: anywhere;
    color: var(--text-med);
  }

  .agents-view__field-error {
    color: var(--red) !important;
  }

  .agents-view__invalid {
    border-color: var(--red) !important;
  }

  @media (max-width: 900px) {
    .agents-view {
      flex-direction: column;
      overflow: visible;
    }

    .agents-view__list-pane {
      width: 100%;
      min-width: 0;
      max-height: 260px;
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }

    .agents-view__detail-pane {
      overflow: visible;
    }

    .agents-view__fields {
      grid-template-columns: 1fr;
    }
  }
</style>
