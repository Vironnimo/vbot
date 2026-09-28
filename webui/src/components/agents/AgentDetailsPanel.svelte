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
      {t('agents.storageDetails')}
    </h3>
  </summary>
  <div class="s-section__body">
    <div class="s-group">
      <div class="s-row">
        <div class="s-row-info">
          <label class="s-row-label" for="agent-id">
            {t('agents.form.id')}
          </label>
          <div class="s-row-desc" id="agent-id-help">
            {t('agents.form.idHelp')}
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
              {t('agents.rename.action')}
            </Button>
          {/if}
        </div>
      </div>

      <div class="s-row s-row--stacked">
        <div class="s-row-info">
          <label class="s-row-label" for="agent-workspace">
            {t('agents.form.workspace')}
          </label>
          <div class="s-row-desc" id="agent-workspace-help">
            {formMode === AGENT_FORM_MODE_CREATE
              ? t('agents.form.workspaceAssignedByServer')
              : t('agents.form.workspaceEditableHelp')}
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
              {t('agents.form.workspaceSetToDefault')}
            </Button>
          {/if}
        </div>
      </div>
    </div>

    <div class="s-group">
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">
            {t('agents.detail.sessionId')}
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
          <div class="s-row-label">{t('agents.detail.created')}</div>
        </div>
        <div class="s-row-control agents-view__meta-value">
          {displayTimestamp(agent?.created_at)}
        </div>
      </div>
      <div class="s-row s-row--compact">
        <div class="s-row-info">
          <div class="s-row-label">{t('agents.detail.updated')}</div>
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
              {t('agents.deleteTitle')}
            </div>
            <div class="s-row-desc">
              {canDeleteSelectedAgent
                ? t('agents.deleteDescription')
                : t('agents.deleteDisabledMinimum')}
            </div>
          </div>
          <div class="s-row-control">
            <Button
              variant="danger"
              disabled={isDeleting || !canDeleteSelectedAgent}
              onClick={deleteSelectedAgent}
            >
              {isDeleting ? t('common.loading') : t('agents.delete')}
            </Button>
          </div>
        </div>
      </div>
    {/if}
  </div>
</details>
