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
      selectAgent(resolveSelectedAgentId(agents, preferredAgentId));
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

  function selectAgent(agentId) {
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
  }

  function startCreate() {
    selectedAgentId = '';
    formMode = AGENT_FORM_MODE_CREATE;
    formValues = createAgentFormValues();
    formErrors = {};
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
  <div class="agents-view__header">
    <p class="agents-view__status">{t('app.ready', 'Ready')}</p>
    <div>
      <h2 id="agents-view-title">{t('agents.title', 'Agents')}</h2>
      <p>
        {t(
          'agents.subtitle',
          'Create and maintain the agent configurations used by chat.',
        )}
      </p>
    </div>
    <button
      class="agents-view__ghost-button"
      type="button"
      onclick={loadAgents}
    >
      {t('common.refresh', 'Refresh')}
    </button>
  </div>

  {#if errorMessage}
    <p class="agents-view__notice agents-view__notice--error" role="alert">
      {errorMessage}
    </p>
  {/if}

  {#if statusMessage}
    <p class="agents-view__notice" role="status">{statusMessage}</p>
  {/if}

  <div class="agents-view__grid">
    <aside class="agents-view__panel" aria-labelledby="agents-list-title">
      <div class="agents-view__panel-header">
        <h3 id="agents-list-title">
          {t('agents.listTitle', 'Available agents')}
        </h3>
        <button type="button" onclick={startCreate}
          >{t('common.new', 'New')}</button
        >
      </div>

      {#if isLoading}
        <p class="agents-view__muted">
          {t('agents.loading', 'Loading agents…')}
        </p>
      {:else if agents.length === 0}
        <p class="agents-view__muted">
          {t('agents.empty', 'No agents found.')}
        </p>
      {:else}
        <div class="agents-view__list">
          {#each agents as agent (agent.id)}
            <button
              class:agents-view__agent-card--active={agent.id ===
                selectedAgentId}
              class="agents-view__agent-card"
              type="button"
              onclick={() => selectAgent(agent.id)}
            >
              <span>{agent.name || agent.id}</span>
              <small>{agent.id}</small>
            </button>
          {/each}
        </div>
      {/if}
    </aside>

    <form class="agents-view__panel agents-view__form" onsubmit={saveAgent}>
      <div class="agents-view__panel-header">
        <h3>{formTitle}</h3>
        {#if formMode === AGENT_FORM_MODE_EDIT}
          <button type="button" onclick={startCreate}
            >{t('agents.create', 'Create Agent')}</button
          >
        {/if}
      </div>

      <label>
        <span>{t('agents.form.id', 'Agent ID')}</span>
        <input
          class:agents-view__invalid={formErrors.id}
          type="text"
          bind:value={formValues.id}
          disabled={formMode === AGENT_FORM_MODE_EDIT}
          aria-describedby="agent-id-help agent-id-error"
        />
        <small id="agent-id-help"
          >{t(
            'agents.form.idHelp',
            'Agent IDs are immutable after creation.',
          )}</small
        >
        {#if formErrors.id}
          <small id="agent-id-error" class="agents-view__field-error"
            >{fieldError('id')}</small
          >
        {/if}
      </label>

      <label>
        <span>{t('agents.form.name', 'Name')}</span>
        <input
          class:agents-view__invalid={formErrors.name}
          type="text"
          bind:value={formValues.name}
        />
        {#if formErrors.name}
          <small class="agents-view__field-error">{fieldError('name')}</small>
        {/if}
      </label>

      <div class="agents-view__two-column">
        <label>
          <span>{t('agents.form.model', 'Model')}</span>
          <input type="text" bind:value={formValues.model} />
        </label>

        <label>
          <span>{t('agents.form.fallbackModel', 'Fallback model')}</span>
          <input type="text" bind:value={formValues.fallback_model} />
        </label>
      </div>

      <label>
        <span>{t('agents.form.workspace', 'Workspace')}</span>
        <input type="text" bind:value={formValues.workspace} />
      </label>

      <div class="agents-view__two-column">
        <label>
          <span>{t('agents.form.temperature', 'Temperature')}</span>
          <input
            class:agents-view__invalid={formErrors.temperature}
            type="number"
            step="0.01"
            bind:value={formValues.temperature}
          />
          {#if formErrors.temperature}
            <small class="agents-view__field-error"
              >{fieldError('temperature')}</small
            >
          {/if}
        </label>

        <label>
          <span>{t('agents.form.thinkingEffort', 'Thinking effort')}</span>
          <input type="text" bind:value={formValues.thinking_effort} />
        </label>
      </div>

      <div class="agents-view__two-column">
        <label>
          <span>{t('agents.form.allowedTools', 'Allowed tools')}</span>
          <textarea rows="5" bind:value={formValues.allowed_tools}></textarea>
          <small>{t('agents.form.listHelp', 'Enter one item per line.')}</small>
        </label>

        <label>
          <span>{t('agents.form.allowedSkills', 'Allowed skills')}</span>
          <textarea rows="5" bind:value={formValues.allowed_skills}></textarea>
          <small>{t('agents.form.listHelp', 'Enter one item per line.')}</small>
        </label>
      </div>

      <div class="agents-view__actions">
        <button type="submit" disabled={isSaving}>
          {isSaving ? t('common.saving', 'Saving…') : submitLabel}
        </button>

        {#if formMode === AGENT_FORM_MODE_EDIT}
          <button
            class="agents-view__danger-button"
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
  </div>
</section>

<style>
  .agents-view {
    width: min(100%, 72rem);
    color: var(--color-text);
  }

  .agents-view__header,
  .agents-view__panel,
  .agents-view__notice {
    border: 1px solid var(--color-border);
    background:
      linear-gradient(135deg, rgba(33, 29, 23, 0.96), rgba(20, 23, 27, 0.9)),
      var(--color-panel);
    box-shadow: 0 2rem 5rem rgba(0, 0, 0, 0.3);
  }

  .agents-view__header {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: var(--space-lg);
    margin-bottom: var(--space-lg);
    padding: var(--space-xl);
    border-radius: var(--radius-lg);
  }

  .agents-view__status {
    margin: 0;
    color: var(--color-accent);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .agents-view h2,
  .agents-view h3,
  .agents-view p {
    margin: 0;
  }

  .agents-view h2 {
    font-size: clamp(3rem, 9vw, 6rem);
    line-height: 0.95;
  }

  .agents-view h3 {
    font-size: 1.35rem;
  }

  .agents-view__header p:last-child,
  .agents-view__muted,
  .agents-view small {
    color: var(--color-muted);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    line-height: 1.5;
  }

  .agents-view__grid {
    display: grid;
    grid-template-columns: minmax(14rem, 20rem) minmax(0, 1fr);
    gap: var(--space-lg);
    align-items: start;
  }

  .agents-view__panel {
    padding: var(--space-lg);
    border-radius: var(--radius-lg);
  }

  .agents-view__panel-header,
  .agents-view__actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .agents-view__list,
  .agents-view__form {
    display: grid;
    gap: var(--space-md);
  }

  .agents-view__list {
    margin-top: var(--space-md);
  }

  .agents-view__agent-card,
  .agents-view button,
  .agents-view input,
  .agents-view textarea {
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
  }

  .agents-view__agent-card,
  .agents-view button {
    color: var(--color-text);
    background: rgba(240, 164, 58, 0.08);
    cursor: pointer;
  }

  .agents-view button {
    padding: 0.7rem 0.95rem;
  }

  .agents-view button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .agents-view__agent-card {
    display: grid;
    width: 100%;
    gap: var(--space-xs);
    padding: var(--space-md);
    text-align: left;
  }

  .agents-view__agent-card--active {
    border-color: var(--color-accent-strong);
    background: var(--color-panel-strong);
  }

  .agents-view__form label {
    display: grid;
    gap: var(--space-xs);
  }

  .agents-view__form label span {
    color: var(--color-accent-strong);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .agents-view input,
  .agents-view textarea {
    width: 100%;
    padding: 0.75rem 0.85rem;
    color: var(--color-text);
    background: rgba(21, 19, 15, 0.82);
    font: inherit;
  }

  .agents-view textarea {
    resize: vertical;
  }

  .agents-view input:disabled {
    color: var(--color-subtle);
  }

  .agents-view__two-column {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-md);
  }

  .agents-view__notice {
    margin-bottom: var(--space-md);
    padding: var(--space-md);
    border-radius: var(--radius-md);
    color: var(--color-accent-strong);
  }

  .agents-view__notice--error,
  .agents-view__field-error {
    color: #ffb199;
  }

  .agents-view__invalid {
    border-color: #ffb199 !important;
  }

  .agents-view__danger-button {
    border-color: rgba(255, 177, 153, 0.5) !important;
    color: #ffd8cc !important;
  }

  .agents-view__ghost-button {
    background: transparent !important;
  }

  @media (max-width: 980px) {
    .agents-view__grid,
    .agents-view__two-column {
      grid-template-columns: 1fr;
    }

    .agents-view__header {
      align-items: start;
      flex-direction: column;
    }
  }
</style>
