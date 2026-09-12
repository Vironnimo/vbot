<script>
  import {
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    agentIdValidationError,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';
  import TabList from '../ui/TabList.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import Modal from '../ui/Modal.svelte';
  import FormField from '../ui/FormField.svelte';
  import TextField from '../ui/TextField.svelte';
  import { onDestroy, untrack } from 'svelte';
  import {
    createAgent,
    deleteAgent,
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
    availableModels = [],
    availableConnections = [],
    availableTools = [],
    availableSkills = [],
    invalidSkills = [],
    availableAgentTargets = [],
    agentTargetCatalogError = '',
    projectOptions = [],
    projectCatalogError = '',
    loadError = '',
    onAgentUpdated = () => {},
    onAgentRenamed = () => {},
    onAgentCreated = async () => {},
    onAgentDeleted = async () => {},
    onToast = () => {},
    onModelDropdownOpenChange = () => {},
    onNavigateToSettingsPanel = () => {},
    onNavigateToAgentPrompt = () => {},
    memoriesRefreshToken = 0,
  } = $props();

  const initialAgent = untrack(() => agent);
  const initialFormMode = initialAgent
    ? AGENT_FORM_MODE_EDIT
    : AGENT_FORM_MODE_CREATE;
  const editorAgentId = initialAgent?.id ?? '';

  let activeDetail = $state('overview');

  let detailScroll = $state(null);
  let detailTabs = $derived([
    { id: 'overview', label: t('management.overview', 'Overview') },
    { id: 'behavior', label: t('management.behavior', 'Behavior') },
    { id: 'access', label: t('management.access', 'Tools & Skills') },
    { id: 'details', label: t('management.details', 'Details') },
  ]);
  function selectDetail(id) {
    return autosaveContext.requestTransition(() => {
      activeDetail = id;
      if (detailScroll) detailScroll.scrollTop = 0;
    });
  }
  let formMode = $state(initialFormMode);
  let formValues = $state(createAgentFormValues(initialAgent ?? {}));
  let editBaselineValues = $state(createAgentFormValues(initialAgent ?? {}));
  let formErrors = $state({});
  let isSaving = $state(false);
  let isDeleting = $state(false);
  let errorMessage = $state('');
  let destroyed = false;
  // Open state for the "disable custom prompt while customizations exist" confirm.
  // Set only when the user switches the toggle off and the agent's scope reports
  // has_customizations; the toggle is reverted first and re-applied on confirm.
  let disableCustomPromptConfirmOpen = $state(false);
  let workspaceDecisionOpen = $state(false);
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
      ? t('agents.form.submitCreate', 'Create agent')
      : t('agents.form.submitUpdate', 'Save changes'),
  );
  let detailSubtitle = $derived(
    formMode === AGENT_FORM_MODE_CREATE
      ? t('agents.detail.newSubtitle', 'id assigned at creation')
      : t('agents.detail.idValue', 'id: {id}', {
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
    hasChanges: agentAutosaveHasChanges,
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
        activeDetail =
          result.errors.id || result.errors.workspace ? 'details' : 'overview';
        errorMessage = t(
          'errors.validation',
          'Check the highlighted fields and try again.',
        );
      }
      return false;
    }

    if (
      formMode === AGENT_FORM_MODE_EDIT &&
      !agentPayloadHasChanges(result.payload)
    ) {
      if (source === 'manual') {
        showAgentToast(t('common.alreadySaved', 'Already saved'));
      }

      return true;
    }

    if (
      source === 'manual' &&
      formMode === AGENT_FORM_MODE_EDIT &&
      Object.hasOwn(result.payload, 'workspace') &&
      options.workspaceCopyChoice === undefined
    ) {
      workspaceDecisionOpen = true;
      return false;
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
        showAgentToast(t('agents.created', 'Agent created.'));
        await onAgentCreated(savedAgent.id ?? result.payload.id);
      } else {
        const updatedSelectedAgent = applySavedAgentUpdate(
          savedAgent,
          result.payload,
          draftValues,
        );
        if (updatedSelectedAgent && source === 'manual') {
          showAgentToast(t('agents.updated', 'Agent updated.'));
        }
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

    return agentAutosaveHasChanges();
  }

  function agentAutosaveHasChanges() {
    if (formMode !== AGENT_FORM_MODE_EDIT) {
      return false;
    }

    const result = normalizeAgentForm(formValues, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues: editBaselineValues,
    });

    if (!result.isValid) {
      return !formValuesMatch(formValues, editBaselineValues);
    }

    return (
      !Object.hasOwn(result.payload, 'workspace') &&
      !Object.hasOwn(result.payload, 'root_project_id') &&
      agentPayloadHasChanges(result.payload)
    );
  }

  function agentPayloadHasChanges(payload) {
    return Object.keys(payload).some((fieldName) => fieldName !== 'id');
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
    void persistAgent(null, {
      source: 'manual',
      workspaceCopyChoice: copyFiles,
    });
  }

  function cancelWorkspaceDecision() {
    workspaceDecisionOpen = false;
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
      return false;
    }

    editBaselineValues = createAgentFormValues(nextAgent);

    if (formValuesMatch(formValues, draftValues)) {
      formValues = createAgentFormValues(nextAgent);
    }

    return true;
  }

  function formValuesMatch(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  async function deleteSelectedAgent() {
    if (!agent) {
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
    errorMessage = '';

    try {
      await deleteAgent(agent.id);
      showAgentToast(t('agents.deleted', 'Agent deleted.'));
      await onAgentDeleted(agent.id);
    } catch (error) {
      errorMessage = viewErrorMessage(error, t('agents.deleteError'));
    } finally {
      isDeleting = false;
    }
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
          ? t('agents.form.required', 'This field is required.')
          : t(
              'agents.rename.invalidId',
              'Use 1–64 letters, numbers, hyphens, or underscores, starting with a letter or number.',
            );
      return;
    }
    if (nextId === agent.id) {
      renameError = t(
        'agents.rename.sameId',
        'Enter an ID different from the current one.',
      );
      return;
    }

    isRenaming = true;
    renameError = '';
    const oldId = agent.id;
    try {
      const renamedAgent = await renameAgent(oldId, nextId);
      renameDialogOpen = false;
      showAgentToast(t('agents.renamed', 'Agent ID changed.'));
      onAgentRenamed(renamedAgent, { oldId, newId: renamedAgent.id ?? nextId });
    } catch (error) {
      renameError = viewErrorMessage(
        error,
        t('agents.renameError', 'Could not change Agent ID.'),
      );
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

<form class="agent-detail-pane" onsubmit={handleAgentSubmit}>
  <div class="management-header">
    <div class="detail-top">
      <div>
        <div class="detail-heading">
          {formMode === AGENT_FORM_MODE_CREATE
            ? t('agents.create', 'Create agent')
            : agent?.name || formValues.name || agent?.id}
        </div>
        <div class="detail-sub">{detailSubtitle}</div>
      </div>
    </div>
    <TabList
      items={detailTabs}
      value={activeDetail}
      idPrefix="agent-detail"
      ariaLabel={t('management.sections', 'Detail sections')}
      onChange={selectDetail}
    />
  </div>
  <div class="agent-detail-scroll" bind:this={detailScroll}>
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
      {activeDetail}
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
      {activeDetail}
      {handleCustomPromptToggle}
      {navigateToAgentPrompt}
      {showAgentToast}
      {viewErrorMessage}
    />
    <AgentAccessPanel
      {availableTools}
      {availableSkills}
      {invalidSkills}
      {availableAgentTargets}
      {agentTargetCatalogError}
      bind:formValues
      {activeDetail}
      {navigateToExtensions}
    />
    <AgentDetailsPanel
      {agent}
      bind:formValues
      {formMode}
      {formErrors}
      {isSaving}
      {isDeleting}
      {activeDetail}
      {deleteSelectedAgent}
      {openRenameDialog}
      {resetWorkspaceToDefault}
      {fieldError}
      {workspaceIsCustom}
      {canDeleteSelectedAgent}
    />
    <div class="agent-detail-footer">
      <Button variant="tertiary" type="submit" disabled={isSaving}>
        {isSaving ? t('common.saving', 'Saving…') : submitLabel}
      </Button>
    </div>
  </div>
</form>

{#if disableCustomPromptConfirmOpen}
  <ConfirmDialog
    danger={false}
    title={t(
      'agents.confirmDisableCustomPrompt.title',
      'Disable custom system prompt?',
    )}
    body={t(
      'agents.confirmDisableCustomPrompt.body',
      'This agent has customized prompt blocks. They will be kept, but the agent stops using them and follows the Default scope again. Re-enabling brings them back.',
    )}
    confirmLabel={t(
      'agents.confirmDisableCustomPrompt.confirm',
      'Disable custom prompt',
    )}
    onConfirm={confirmDisableCustomPrompt}
    onCancel={cancelDisableCustomPrompt}
  />
{/if}

{#if renameDialogOpen}
  <Modal
    title={t('agents.rename.title', 'Change Agent ID?')}
    closeDisabled={isRenaming}
    onClose={closeRenameDialog}
  >
    {#snippet body()}
      <form id="agent-rename-form" onsubmit={renameSelectedAgent}>
        <p>
          {t(
            'agents.rename.body',
            'The complete Identity Agent moves to the new ID, including Sessions, Memory, prompts, private Skills, and its internal Workspace. Live Channels, Cron jobs, delegation policies, and Sub-Agent navigation links are updated. Historical records keep the ID they were created with.',
          )}
        </p>
        <FormField
          controlId="agent-rename-id"
          label={t('agents.rename.newId', 'New Agent ID')}
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
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="primary"
        type="submit"
        form="agent-rename-form"
        loading={isRenaming}
      >
        {isRenaming
          ? t('common.saving', 'Saving…')
          : t('agents.rename.confirm', 'Change ID')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if workspaceDecisionOpen}
  <Modal
    title={t('agents.workspaceMove.title', 'Change Workspace?')}
    onClose={cancelWorkspaceDecision}
  >
    {#snippet body()}
      <p>
        {t(
          'agents.workspaceMove.body',
          'Choose whether to copy SOUL.md, USER.md, and MEMORY.md into the new Workspace. Source files remain in place; existing destination versions are backed up before replacement.',
        )}
      </p>
    {/snippet}
    {#snippet footer()}
      <Button variant="secondary" onClick={cancelWorkspaceDecision}>
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button variant="secondary" onClick={() => chooseWorkspaceCopy(false)}>
        {t('agents.workspaceMove.dontCopy', "Don't copy")}
      </Button>
      <Button variant="primary" onClick={() => chooseWorkspaceCopy(true)}>
        {t('agents.workspaceMove.copy', 'Copy files')}
      </Button>
    {/snippet}
  </Modal>
{/if}
