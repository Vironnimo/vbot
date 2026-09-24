<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import TextField from '../ui/TextField.svelte';
  import {
    AGENT_FORM_MODE_EDIT,
    AGENT_FORM_MODE_CREATE,
  } from '$lib/agentForm.js';
  import Button from '../ui/Button.svelte';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  let {
    agent,
    formValues = $bindable(),
    formMode,
    formErrors,
    isSaving,
    isDeleting,
    deleteSelectedAgent,
    openRenameDialog,
    resetWorkspaceToDefault,
    fieldError,
    workspaceIsCustom,
    canDeleteSelectedAgent,
  } = $props();

  let expanded = $state(false);
  $effect(() => {
    if (formErrors.id || formErrors.workspace) expanded = true;
  });

  const EMPTY_VALUE = '—';

  function displayValue(value) {
    return value || EMPTY_VALUE;
  }

  function displayTimestamp(value) {
    if (!value) {
      return EMPTY_VALUE;
    }

    const parsedValue = Date.parse(value);
    if (Number.isNaN(parsedValue)) {
      return value;
    }

    return formatDateTimeInApplicationZone(
      new Date(parsedValue),
      activeLocaleTag(),
      { dateStyle: 'medium', timeStyle: 'short' },
    );
  }
</script>

<details
  class="s-section agents-view__part agents-view__advanced"
  id="agent-detail-panel-details"
  bind:open={expanded}
>
  <summary class="s-section__head agents-view__advanced-summary">
    <span
      class="disclosure-chevron"
      class:disclosure-chevron--open={expanded}
      aria-hidden="true"
    ></span>
    <h3 class="s-section__title">
      {t('agents.storageDetails', 'Workspace & advanced')}
    </h3>
  </summary>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="agent-id">
            {t('agents.form.id', 'Agent ID')}
          </label>
          <div class="s-row-desc" id="agent-id-help">
            {t(
              'agents.form.idHelp',
              'Used to address this Identity Agent in Sessions, Channels, Cron jobs, and delegation.',
            )}
          </div>
          {#if formErrors.id}
            <p class="agents-view__row-error" id="agent-id-error" role="alert">
              {fieldError('id')}
            </p>
          {/if}
        </div>
        <div class="s-row-control agents-view__field-with-action">
          <TextField
            id="agent-id"
            invalid={Boolean(formErrors.id)}
            value={formValues.id}
            onInput={(next) => (formValues.id = next)}
            disabled={formMode === AGENT_FORM_MODE_EDIT}
            aria-required={formMode === AGENT_FORM_MODE_CREATE || undefined}
            aria-describedby={formErrors.id
              ? 'agent-id-help agent-id-error'
              : 'agent-id-help'}
          />
          {#if formMode === AGENT_FORM_MODE_EDIT}
            <Button
              variant="tertiary"
              onClick={openRenameDialog}
              disabled={isSaving || isDeleting}
            >
              {t('agents.rename.action', 'Change ID')}
            </Button>
          {/if}
        </div>
      </div>

      <div class="s-row s-row--stacked">
        <div class="s-row-info">
          <label class="s-row-label" for="agent-workspace">
            {t('agents.form.workspace', 'Workspace')}
          </label>
          <div class="s-row-desc" id="agent-workspace-help">
            {formMode === AGENT_FORM_MODE_CREATE
              ? t(
                  'agents.form.workspaceAssignedByServer',
                  'Workspace is assigned by the server when the agent is created.',
                )
              : t(
                  'agents.form.workspaceEditableHelp',
                  "Home of this agent's identity and memory files (SOUL.md, USER.md, MEMORY.md); the memory tool works here. File tools follow the session's working directory instead — the project repository in project sessions.",
                )}
          </div>
          {#if formErrors.workspace}
            <p
              class="agents-view__row-error"
              id="agent-workspace-error"
              role="alert"
            >
              {fieldError('workspace')}
            </p>
          {/if}
        </div>
        <div class="agents-view__field-with-action">
          <TextField
            id="agent-workspace"
            code
            invalid={Boolean(formErrors.workspace)}
            value={formValues.workspace}
            onInput={(next) => (formValues.workspace = next)}
            disabled={formMode === AGENT_FORM_MODE_CREATE}
            aria-describedby={formErrors.workspace
              ? 'agent-workspace-help agent-workspace-error'
              : 'agent-workspace-help'}
          />
          {#if workspaceIsCustom}
            <Button
              variant="tertiary"
              class="agents-view__reset-inherit"
              disabled={isSaving || isDeleting}
              onClick={resetWorkspaceToDefault}
            >
              {t('agents.form.workspaceSetToDefault', 'Set to default')}
            </Button>
          {/if}
        </div>
      </div>
    </div>

    <div class="s-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('agents.detail.sessionId', 'Current session ID')}
          </div>
        </div>
        <div
          class="s-row-control agents-view__meta-value agents-view__meta-value--mono"
        >
          {displayValue(agent?.current_session_id)}
        </div>
      </div>
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">{t('agents.detail.created', 'Created')}</div>
        </div>
        <div class="s-row-control agents-view__meta-value">
          {displayTimestamp(agent?.created_at)}
        </div>
      </div>
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">{t('agents.detail.updated', 'Updated')}</div>
        </div>
        <div class="s-row-control agents-view__meta-value">
          {displayTimestamp(agent?.updated_at)}
        </div>
      </div>
    </div>

    {#if formMode === AGENT_FORM_MODE_EDIT}
      <div class="s-group">
        <div class="s-row s-row--compact">
          <div class="s-row-info">
            <div class="s-row-label">
              {t('agents.deleteTitle', 'Delete this Agent')}
            </div>
            <div class="s-row-desc">
              {canDeleteSelectedAgent
                ? t(
                    'agents.deleteDescription',
                    'Moves the Agent to the archive. An Agent that is still referenced or has active Runs cannot be deleted.',
                  )
                : t(
                    'agents.deleteDisabledMinimum',
                    'The last remaining agent cannot be deleted.',
                  )}
            </div>
          </div>
          <div class="s-row-control">
            <Button
              variant="danger"
              disabled={isDeleting || !canDeleteSelectedAgent}
              onClick={deleteSelectedAgent}
            >
              {isDeleting
                ? t('common.loading', 'Loading…')
                : t('agents.delete', 'Delete agent')}
            </Button>
          </div>
        </div>
      </div>
    {/if}
  </div>
</details>
