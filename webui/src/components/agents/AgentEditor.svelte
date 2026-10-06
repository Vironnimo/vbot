<script>
  import {
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    agentIdValidationError,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import SaveStatus from '../ui/SaveStatus.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import Modal from '../ui/Modal.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import { onDestroy, tick, untrack } from 'svelte';
  import {
    createAgent,
    listPrompts,
    renameAgent,
    updateAgent,
  } from '$lib/api.js';
  import {
    createDebouncedAutosave,
    useAutosaveContext,
  } from '$lib/autosave.js';
  import AgentOverviewPanel from './AgentOverviewPanel.svelte';
  import AgentBehaviorPanel from './AgentBehaviorPanel.svelte';
  import AgentAccessPanel from './AgentAccessPanel.svelte';
  import AgentDetailsPanel from './AgentDetailsPanel.svelte';

  let {
    agent = null,
    agentsCount = 0,
    // The view deletes this Agent after its confirmation.
    isDeleting = false,
    availableModels = [],
    availableConnections = [],
    availableTools = [],
    skillCatalog = undefined,
    availableAgentTargets = [],
    agentTargetCatalogError = '',
    projectOptions = [],
    projectCatalogError = '',
    loadError = '',
    onAgentUpdated = () => {},
    onAgentRenamed = () => {},
    onAgentCreated = async () => {},
    onDeleteRequested = () => {},
    onToast = () => {},
    onModelDropdownOpenChange = () => {},
    onNavigateToSettingsPanel = () => {},
    onNavigateToAgentPrompt = () => {},
    // Reloads `skillCatalog` after a Skill write from the Skills section.
    onSkillsChanged = async () => {},
    // Opens a Skill's page in the Skills manager: (agentId, skillId).
    onOpenSkill = () => {},
    memoriesRefreshToken = 0,
  } = $props();

  const initialAgent = untrack(() => agent);
  const initialFormMode = initialAgent
    ? AGENT_FORM_MODE_EDIT
    : AGENT_FORM_MODE_CREATE;
  const editorAgentId = initialAgent?.id ?? '';

  let formMode = $state(initialFormMode);
  let editorForm = $state(null);
  let formValues = $state(createAgentFormValues(initialAgent ?? {}));
  let editBaselineValues = $state(createAgentFormValues(initialAgent ?? {}));
  let formErrors = $state({});
  let isSaving = $state(false);
  let errorMessage = $state('');
  let destroyed = false;
  // Open state for the "disable custom prompt while customizations exist" confirm.
  // Set only when the user switches the toggle off and the agent's scope reports
  // has_customizations; the toggle is reverted first and re-applied on confirm.
  let disableCustomPromptConfirmOpen = $state(false);
  let workspaceDecisionOpen = $state(false);
  // Set while a save made on leaving the editor waits for that decision.
  let resolveWorkspaceDecision = null;
  let renameDialogOpen = $state(false);
  let renameValue = $state('');
  let renameError = $state('');
  let isRenaming = $state(false);

  let canDeleteSelectedAgent = $derived(Boolean(agent) && agentsCount > 1);
  // The agent points at a custom identity/Memory home rather than its
  // default home under the agent folder — gates the "set to default" action.
  let workspaceIsCustom = $derived(
    formMode === AGENT_FORM_MODE_EDIT &&
      Boolean(agent?.workspace) &&
      Boolean(agent?.default_workspace) &&
      agent.workspace !== agent.default_workspace,
  );
  let submitLabel = $derived(
    formMode === AGENT_FORM_MODE_CREATE
      ? t('agents.form.submitCreate')
      : t('agents.form.submitUpdate'),
  );
  let detailSubtitle = $derived(
    formMode === AGENT_FORM_MODE_CREATE
      ? t('agents.detail.newSubtitle')
      : t('agents.detail.idValue', {
          id: agent?.id ?? formValues.id,
        }),
  );

  // The per-field effective value + winning source from the agent payload, so an
  // empty (inherit) field can describe what it inherits. Absent for the create
  // form (no persisted agent yet) — the create modal builds its own labels.
  let effectiveConfig = $derived(
    agent?.effective && typeof agent.effective === 'object'
      ? agent.effective
      : {},
  );

  const autosaveContext = useAutosaveContext();
  const agentSave = createDebouncedAutosave({
    getSnapshot: () => cloneAgentFormValues(formValues),
    hasChanges: agentFormHasChanges,
    save: (source) => persistAgent(null, { source }),
  });
  const agentAutosave = agentSave.participant;
  const unregisterAgentAutosave = autosaveContext.register(agentAutosave);

  $effect(() => {
    if (loadError) {
      errorMessage = loadError;
    }
  });

  $effect(() => {
    if (!shouldAutoSaveAgent()) {
      clearAgentAutoSaveTimer();
      return;
    }

    agentSave.scheduleRun();

    return () => {
      clearAgentAutoSaveTimer();
    };
  });

  onDestroy(() => {
    destroyed = true;

    unregisterAgentAutosave();
    clearAgentAutoSaveTimer();
    settleWorkspaceDecision(null);
  });

  function handleAgentSubmit(event) {
    event?.preventDefault?.();
    void agentAutosave.runSave('manual', { force: true });
  }

  async function persistAgent(event = null, options = {}) {
    event?.preventDefault?.();

    const source = options.source ?? 'manual';
    if (source === 'manual') {
      clearAgentAutoSaveTimer();
    }

    if (isSaving || isDeleting || workspaceDecisionOpen || renameDialogOpen) {
      return false;
    }

    const result = normalizeAgentForm(formValues, {
      mode: formMode,
      initialValues:
        formMode === AGENT_FORM_MODE_EDIT ? editBaselineValues : null,
    });

    if (source !== 'auto') {
      formErrors = result.errors;
      errorMessage = '';
    }

    if (!result.isValid) {
      if (source !== 'auto') {
        errorMessage = t('errors.validation');
        await tick();
        editorForm?.querySelector('[aria-invalid="true"]')?.focus();
      }
      return false;
    }

    if (
      formMode === AGENT_FORM_MODE_EDIT &&
      !agentPayloadHasChanges(result.payload)
    ) {
      return true;
    }

    if (source === 'auto' && changesWorkspaceOrProject(result.payload)) {
      return false;
    }

    if (
      formMode === AGENT_FORM_MODE_EDIT &&
      Object.hasOwn(result.payload, 'workspace') &&
      options.workspaceCopyChoice === undefined
    ) {
      workspaceDecisionOpen = true;
      if (source !== 'transition') {
        return false;
      }
      // Leaving the editor saves the Workspace change once the user decides
      // about its files; Cancel keeps the draft and fails the save.
      const copyFiles = await new Promise((resolve) => {
        resolveWorkspaceDecision = resolve;
      });
      if (copyFiles === null) {
        return false;
      }
      return persistAgent(null, { source, workspaceCopyChoice: copyFiles });
    }

    if (
      Object.hasOwn(result.payload, 'workspace') &&
      options.workspaceCopyChoice !== undefined
    ) {
      result.payload.copy_workspace_identity_files = Boolean(
        options.workspaceCopyChoice,
      );
    }

    isSaving = true;
    const saveMode = formMode;
    const saveAgentId = result.payload.id;
    const draftValues = cloneAgentFormValues(formValues);
    errorMessage = '';

    try {
      const saveAgent =
        saveMode === AGENT_FORM_MODE_CREATE ? createAgent : updateAgent;
      const savedAgent = await saveAgent(result.payload);
      if (saveMode === AGENT_FORM_MODE_CREATE) {
        showAgentToast(t('agents.created'));
        await onAgentCreated(savedAgent.id ?? result.payload.id);
      } else {
        applySavedAgentUpdate(savedAgent, result.payload, draftValues);
      }

      return true;
    } catch (error) {
      if (
        saveMode === AGENT_FORM_MODE_CREATE ||
        (!destroyed && editorAgentId === saveAgentId)
      ) {
        errorMessage = viewErrorMessage(error, t('agents.saveError'));
      }
      return false;
    } finally {
      isSaving = false;
    }
  }

  function shouldAutoSaveAgent() {
    if (
      formMode !== AGENT_FORM_MODE_EDIT ||
      isSaving ||
      isDeleting ||
      workspaceDecisionOpen ||
      renameDialogOpen ||
      destroyed
    ) {
      return false;
    }

    const result = editDraft();
    return result.isValid
      ? agentPayloadHasChanges(result.payload) &&
          !changesWorkspaceOrProject(result.payload)
      : !formValuesMatch(formValues, editBaselineValues);
  }

  // Every unsaved edit, also one automatic saves skip: navigation and leaving
  // the page save it first or ask to discard it.
  function agentFormHasChanges() {
    if (formMode !== AGENT_FORM_MODE_EDIT) {
      return false;
    }
    const result = editDraft();
    return result.isValid
      ? agentPayloadHasChanges(result.payload)
      : !formValuesMatch(formValues, editBaselineValues);
  }

  function editDraft() {
    return normalizeAgentForm(formValues, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues: editBaselineValues,
    });
  }

  function agentPayloadHasChanges(payload) {
    return Object.keys(payload).some((fieldName) => fieldName !== 'id');
  }

  // A Workspace or Project change saves only explicitly or when the user
  // leaves the editor, never automatically.
  function changesWorkspaceOrProject(payload) {
    return (
      Object.hasOwn(payload, 'workspace') ||
      Object.hasOwn(payload, 'root_project_id')
    );
  }

  async function resetWorkspaceToDefault() {
    if (!agent?.default_workspace || isSaving || isDeleting) {
      return;
    }

    // Repoint the workspace to the default home and persist. Files at the
    // previous custom location are left untouched — it may be a repo the agent
    // was rooted in — so this only changes which directory the agent uses.
    formValues.workspace = agent.default_workspace;
    await agentAutosave.runSave('manual', { force: true });
  }

  function chooseWorkspaceCopy(copyFiles) {
    workspaceDecisionOpen = false;
    if (settleWorkspaceDecision(copyFiles)) {
      return;
    }
    void persistAgent(null, {
      source: 'manual',
      workspaceCopyChoice: copyFiles,
    });
  }

  function cancelWorkspaceDecision() {
    workspaceDecisionOpen = false;
    settleWorkspaceDecision(null);
  }

  // Answers a save that waits for the Workspace decision; false when none waits.
  function settleWorkspaceDecision(copyFiles) {
    const resolve = resolveWorkspaceDecision;
    resolveWorkspaceDecision = null;
    resolve?.(copyFiles);
    return resolve !== null;
  }

  function clearAgentAutoSaveTimer() {
    agentSave.cancelPendingTimer();
  }

  function showAgentToast(title, variant = 'success') {
    onToast({ title, variant });
  }

  function cloneAgentFormValues(values) {
    return {
      ...values,
      allowed_skills: Array.isArray(values.allowed_skills)
        ? [...values.allowed_skills]
        : [],
      excluded_skills: Array.isArray(values.excluded_skills)
        ? [...values.excluded_skills]
        : [],
      tool_access: cloneTools(values.tool_access),
      tools: cloneTools(values.tools),
    };
  }

  function applySavedAgentUpdate(savedAgent, payload, draftValues) {
    const existingAgent = agent ?? {};
    const nextAgent = {
      ...existingAgent,
      ...payload,
      ...(savedAgent ?? {}),
      id: savedAgent?.id ?? payload.id ?? existingAgent.id,
    };

    onAgentUpdated(nextAgent, { notifySelection: !destroyed });

    if (
      destroyed ||
      formMode !== AGENT_FORM_MODE_EDIT ||
      editorAgentId !== nextAgent.id
    ) {
      return;
    }

    editBaselineValues = createAgentFormValues(nextAgent);

    if (formValuesMatch(formValues, draftValues)) {
      formValues = createAgentFormValues(nextAgent);
    }
  }

  function formValuesMatch(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function deleteSelectedAgent() {
    if (agent && canDeleteSelectedAgent) onDeleteRequested(agent);
  }

  function openRenameDialog() {
    if (!agent || isSaving || isDeleting) {
      return;
    }
    autosaveContext.requestTransition(openRenameDialogAfterSave);
  }

  function openRenameDialogAfterSave() {
    clearAgentAutoSaveTimer();
    renameValue = agent.id;
    renameError = '';
    renameDialogOpen = true;
  }

  function closeRenameDialog() {
    if (isRenaming) {
      return;
    }
    renameDialogOpen = false;
    renameError = '';
  }

  async function renameSelectedAgent(event = null) {
    event?.preventDefault?.();
    if (!agent || isRenaming) {
      return;
    }
    const nextId = renameValue.trim();
    const validationError = agentIdValidationError(nextId);
    if (validationError) {
      renameError =
        validationError === 'required'
          ? t('agents.form.required')
          : t('agents.rename.invalidId');
      return;
    }
    if (nextId === agent.id) {
      renameError = t('agents.rename.sameId');
      return;
    }

    isRenaming = true;
    renameError = '';
    const oldId = agent.id;
    try {
      const renamedAgent = await renameAgent(oldId, nextId);
      renameDialogOpen = false;
      showAgentToast(t('agents.renamed'));
      onAgentRenamed(renamedAgent, { oldId, newId: renamedAgent.id ?? nextId });
    } catch (error) {
      renameError = viewErrorMessage(error, t('agents.renameError'));
    } finally {
      isRenaming = false;
    }
  }

  function cloneTools(tools) {
    return tools && typeof tools === 'object'
      ? JSON.parse(JSON.stringify(tools))
      : {};
  }

  function inheritSource(fieldName) {
    const field = effectiveConfig[fieldName];
    return field && typeof field === 'object' ? (field.source ?? null) : null;
  }

  function inheritDisplayValue(fieldName) {
    const field = effectiveConfig[fieldName];
    const value = field && typeof field === 'object' ? field.value : null;
    return value === null || value === undefined ? '' : String(value);
  }

  function navigateToAgentDefaults() {
    onNavigateToSettingsPanel('defaults');
  }

  function navigateToExtensions(_extensionName) {
    onNavigateToSettingsPanel('extensions');
  }

  function navigateToAgentPrompt() {
    if (agent?.id) {
      onNavigateToAgentPrompt(agent.id);
    }
  }

  // Turning the custom-prompt toggle off while the agent's scope owns customized
  // blocks opens a confirm first (the blocks are kept, just no longer used).
  // Turning it on, or off with no customizations / a failed scope fetch, applies
  // immediately with no dialog.
  async function handleCustomPromptToggle(next) {
    if (next) {
      formValues.custom_system_prompt_enabled = true;
      return;
    }

    if (!(await agentPromptScopeHasCustomizations())) {
      formValues.custom_system_prompt_enabled = false;
      return;
    }

    disableCustomPromptConfirmOpen = true;
  }

  async function agentPromptScopeHasCustomizations() {
    const agentId = agent?.id;
    if (!agentId) {
      return false;
    }
    try {
      const result = await listPrompts();
      const scopes = Array.isArray(result?.scopes) ? result.scopes : [];
      const scope = scopes.find(
        (item) => item?.type === 'agent' && item.agent_id === agentId,
      );
      return scope?.has_customizations === true;
    } catch {
      // A failed scope fetch must not block disabling — apply without the dialog.
      return false;
    }
  }

  function confirmDisableCustomPrompt() {
    disableCustomPromptConfirmOpen = false;
    formValues.custom_system_prompt_enabled = false;
  }

  function cancelDisableCustomPrompt() {
    // Cancel reverts the toggle — it never left the on state in the form, so this
    // just closes the dialog.
    disableCustomPromptConfirmOpen = false;
  }

  function fieldError(fieldName) {
    if (!formErrors[fieldName]) {
      return '';
    }

    if (formErrors[fieldName] === 'required') {
      return t('agents.form.required');
    }

    return t('errors.validation');
  }

  function viewErrorMessage(error, fallback) {
    return error?.message || fallback || t('errors.generic');
  }
</script>

<form
  class="agent-detail-pane"
  bind:this={editorForm}
  onsubmit={handleAgentSubmit}
>
  <div class="agent-detail-scroll page-scroll">
    <div class="management-header">
      <div class="detail-top">
        <div>
          <h2 class="detail-heading view-header__title">
            {formMode === AGENT_FORM_MODE_CREATE
              ? t('agents.create')
              : agent?.name || formValues.name || agent?.id}
          </h2>
          <div class="detail-sub">{detailSubtitle}</div>
        </div>
      </div>
    </div>
    {#if errorMessage}
      <Banner variant="error" role="alert">
        {errorMessage}
      </Banner>
    {/if}

    <AgentOverviewPanel
      {availableModels}
      {availableConnections}
      {projectOptions}
      {projectCatalogError}
      {onModelDropdownOpenChange}
      bind:formValues
      {formMode}
      {formErrors}
      {fieldError}
      {navigateToAgentDefaults}
      {inheritSource}
      {inheritDisplayValue}
    />
    <AgentBehaviorPanel
      {agent}
      {onNavigateToSettingsPanel}
      {memoriesRefreshToken}
      bind:formValues
      {formMode}
      {handleCustomPromptToggle}
      {navigateToAgentPrompt}
      {showAgentToast}
      {viewErrorMessage}
    />
    <AgentAccessPanel
      {availableTools}
      {skillCatalog}
      {availableAgentTargets}
      {agentTargetCatalogError}
      bind:formValues
      {navigateToExtensions}
      {onToast}
      {onSkillsChanged}
      {onOpenSkill}
    />
    <AgentDetailsPanel
      {agent}
      bind:formValues
      {formMode}
      {formErrors}
      {isSaving}
      {isDeleting}
      {deleteSelectedAgent}
      {openRenameDialog}
      {resetWorkspaceToDefault}
      {fieldError}
      {workspaceIsCustom}
      {canDeleteSelectedAgent}
    />
    <div class="agent-detail-footer">
      {#if formMode === AGENT_FORM_MODE_EDIT}
        <SaveStatus
          type="submit"
          saving={isSaving}
          pending={!formValuesMatch(formValues, editBaselineValues)}
        />
      {:else}
        <Button variant="tertiary" type="submit" disabled={isSaving}>
          {isSaving ? t('common.saving') : submitLabel}
        </Button>
      {/if}
    </div>
  </div>
</form>

{#if disableCustomPromptConfirmOpen}
  <ConfirmDialog
    danger={false}
    title={t('agents.confirmDisableCustomPrompt.title')}
    body={t('agents.confirmDisableCustomPrompt.body')}
    confirmLabel={t('agents.confirmDisableCustomPrompt.confirm')}
    onConfirm={confirmDisableCustomPrompt}
    onCancel={cancelDisableCustomPrompt}
  />
{/if}

{#if renameDialogOpen}
  <Modal
    title={t('agents.rename.title')}
    closeDisabled={isRenaming}
    onClose={closeRenameDialog}
  >
    {#snippet body()}
      <form id="agent-rename-form" onsubmit={renameSelectedAgent}>
        <p>
          {t('agents.rename.body')}
        </p>
        <FormField
          controlId="agent-rename-id"
          label={t('agents.rename.newId')}
          required
          error={renameError}
        >
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              value={renameValue}
              onInput={(next) => {
                renameValue = next;
                renameError = '';
              }}
              invalid={field.invalid}
              disabled={isRenaming}
              aria-describedby={field.describedBy}
              autofocus
            />
          {/snippet}
        </FormField>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        onClick={closeRenameDialog}
        disabled={isRenaming}
      >
        {t('common.cancel')}
      </Button>
      <Button
        variant="primary"
        type="submit"
        form="agent-rename-form"
        loading={isRenaming}
      >
        {isRenaming ? t('common.saving') : t('agents.rename.confirm')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if workspaceDecisionOpen}
  <Modal
    title={t('agents.workspaceMove.title')}
    onClose={cancelWorkspaceDecision}
  >
    {#snippet body()}
      <p>
        {t('agents.workspaceMove.body')}
      </p>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={cancelWorkspaceDecision}>
        {t('common.cancel')}
      </Button>
      <Button variant="secondary" onClick={() => chooseWorkspaceCopy(false)}>
        {t('agents.workspaceMove.dontCopy')}
      </Button>
      <Button variant="primary" onClick={() => chooseWorkspaceCopy(true)}>
        {t('agents.workspaceMove.copy')}
      </Button>
    {/snippet}
  </Modal>
{/if}
