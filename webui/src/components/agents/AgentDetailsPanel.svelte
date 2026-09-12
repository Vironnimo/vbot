<script>
  import { t, activeLocaleTag } from '$lib/i18n.js';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    AGENT_FORM_MODE_EDIT,
    AGENT_FORM_MODE_CREATE,
  } from '$lib/agentForm.js';
  import Button from '../ui/Button.svelte';
  import { tooltip } from '$lib/tooltip.js';
  import { formatDateTimeInApplicationZone } from '$lib/dateTimePrefs.svelte.js';
  let {
    agent,
    formValues = $bindable(),
    formMode,
    formErrors,
    isSaving,
    isDeleting,
    activeDetail,
    deleteSelectedAgent,
    openRenameDialog,
    resetWorkspaceToDefault,
    fieldError,
    workspaceIsCustom,
    canDeleteSelectedAgent,
  } = $props();

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

<div
  class="management-topic"
  role="tabpanel"
  id="agent-detail-panel-details"
  aria-labelledby="agent-detail-tab-details"
  hidden={activeDetail !== 'details'}
  tabindex="0"
>
  <div class="detail-group">
    <div class="detail-group-title">
      {t('management.details', 'Details')}
    </div>
    <div class="detail-fields">
      <FormField
        controlId="agent-id"
        label={t('agents.form.id', 'Agent ID')}
        required
        help={t(
          'agents.form.idHelp',
          'Used to address this Identity Agent in Sessions, Channels, Cron jobs, and delegation.',
        )}
        error={formErrors.id ? fieldError('id') : ''}
      >
        {#snippet children(field)}
          <TextField
            id={field.controlId}
            invalid={field.invalid}
            value={formValues.id}
            onInput={(next) => (formValues.id = next)}
            disabled={formMode === AGENT_FORM_MODE_EDIT}
            aria-describedby={field.describedBy}
          />
        {/snippet}
        {#snippet actions()}
          {#if formMode === AGENT_FORM_MODE_EDIT}
            <Button
              variant="tertiary"
              onClick={openRenameDialog}
              disabled={isSaving || isDeleting}
            >
              {t('agents.rename.action', 'Change ID')}
            </Button>
          {/if}
        {/snippet}
      </FormField>
      <FormField
        controlId="agent-workspace"
        full
        label={t('agents.form.workspace', 'Workspace')}
        help={formMode === AGENT_FORM_MODE_CREATE
          ? t(
              'agents.form.workspaceAssignedByServer',
              'Workspace is assigned by the server when the agent is created.',
            )
          : t(
              'agents.form.workspaceEditableHelp',
              "Home of this agent's identity and memory files (SOUL.md, USER.md, MEMORY.md); the memory tool works here. File tools follow the session's working directory instead — the project repository in project sessions.",
            )}
        error={formErrors.workspace ? fieldError('workspace') : ''}
      >
        {#snippet children(field)}
          <TextField
            id={field.controlId}
            class="mono"
            invalid={field.invalid}
            value={formValues.workspace}
            onInput={(next) => (formValues.workspace = next)}
            disabled={formMode === AGENT_FORM_MODE_CREATE}
            aria-describedby={field.describedBy}
          />
        {/snippet}
        {#snippet actions()}
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
        {/snippet}
      </FormField>
    </div>
  </div>
  <div class="detail-group">
    <div class="detail-group-title">
      {t('agents.detail.metadata', 'Metadata')}
    </div>
    <div class="detail-fields">
      <div class="f wide">
        <div class="f-label">
          {t('agents.detail.sessionId', 'Current session ID')}
        </div>
        <div class="f-value mono agents-view__wrap-value">
          {displayValue(agent?.current_session_id)}
        </div>
      </div>
      <div class="f">
        <div class="f-label">{t('agents.detail.created', 'Created')}</div>
        <div class="f-value mono agents-view__wrap-value">
          {displayTimestamp(agent?.created_at)}
        </div>
      </div>
      <div class="f">
        <div class="f-label">{t('agents.detail.updated', 'Updated')}</div>
        <div class="f-value mono agents-view__wrap-value">
          {displayTimestamp(agent?.updated_at)}
        </div>
      </div>
    </div>
  </div>

  <div class="detail-btns">
    {#if formMode === AGENT_FORM_MODE_EDIT}
      <!-- The disabled reason must show on the *disabled* button, which
               receives no pointer events — so the tooltip listens on this
               wrapper span. -->
      <span
        class="tooltip-anchor"
        use:tooltip={!canDeleteSelectedAgent
          ? t(
              'agents.deleteDisabledMinimum',
              'The last remaining agent cannot be deleted.',
            )
          : ''}
      >
        <Button
          variant="danger"
          disabled={isDeleting || !canDeleteSelectedAgent}
          onClick={deleteSelectedAgent}
        >
          {isDeleting
            ? t('common.loading', 'Loading…')
            : t('agents.delete', 'Delete agent')}
        </Button>
      </span>
    {/if}
  </div>
</div>
