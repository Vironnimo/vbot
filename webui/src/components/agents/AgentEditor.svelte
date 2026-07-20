<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import CompactionPolicyEditor from '../compaction/CompactionPolicyEditor.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import ConfirmDialog from '../ui/ConfirmDialog.svelte';
  import FormField from '../ui/FormField.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import ToggleChipList from '../ui/ToggleChipList.svelte';
  import {
    createAgent,
    deleteAgent,
    listPrompts,
    updateAgent,
  } from '$lib/api.js';
  import {
    AGENT_MEMORY_PROMPT_MODES,
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    MEMORY_TOOL_NAME,
    createAgentFormValues,
    effortOptionsForReasoning,
    normalizeAgentForm,
    reasoningForModelValue,
    subagentAllowedAgents,
    withSubagentAllowedAgents,
  } from '$lib/agentForm.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';
  import { tooltip } from '$lib/tooltip.js';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';

  const EMPTY_VALUE = '—';
  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const timestampFormatter = new Intl.DateTimeFormat(activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
  const WILDCARD_ACCESS = '*';

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
    onAgentCreated = async () => {},
    onAgentDeleted = async () => {},
    onToast = () => {},
    onModelDropdownOpenChange = () => {},
    onNavigateToSettingsPanel = () => {},
    onNavigateToAgentPrompt = () => {},
  } = $props();

  const initialAgent = untrack(() => agent);
  const initialFormMode = initialAgent
    ? AGENT_FORM_MODE_EDIT
    : AGENT_FORM_MODE_CREATE;
  const editorAgentId = initialAgent?.id ?? '';

  let formMode = $state(initialFormMode);
  let formValues = $state(createAgentFormValues(initialAgent ?? {}));
  let editBaselineValues = $state(createAgentFormValues(initialAgent ?? {}));
  let formErrors = $state({});
  let isSaving = $state(false);
  let isDeleting = $state(false);
  let errorMessage = $state('');
  let agentAutoSaveTimer = null;
  let destroyed = false;
  // Open state for the "disable custom prompt while customizations exist" confirm.
  // Set only when the user switches the toggle off and the agent's scope reports
  // has_customizations; the toggle is reverted first and re-applied on confirm.
  let disableCustomPromptConfirmOpen = $state(false);
  let workspaceDecisionOpen = $state(false);

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
  let visibleToolItems = $derived(toolAccessItems());
  let visibleSkillItems = $derived(skillAccessItems());
  let visibleAgentTargetItems = $derived(agentTargetAccessItems());
  // Memory is a display-only, never-a-toggle chip (a "locked" chip with an "auto"
  // tag): it follows the Memory setting, not the allow-list. Rendered first in the
  // tools cloud; its hover card carries the description and the follows-setting note.
  let memoryChipItem = $derived(
    memoryToolItem
      ? {
          name: memoryToolItem.name,
          description: memoryToolItem.description,
          allowed: formValues.memory_prompt_mode !== 'off',
          locked: true,
          lockedNote: memoryToolRowText(),
        }
      : null,
  );
  // The shared chip list keys off `allowed`; the access items track it as
  // `isAllowed`, so map it across (everything else — description, readiness,
  // warnings — passes through unchanged). Memory (locked) leads the tools cloud.
  let toolChipItems = $derived([
    ...(memoryChipItem ? [memoryChipItem] : []),
    ...visibleToolItems.map((tool) => ({ ...tool, allowed: tool.isAllowed })),
  ]);
  let skillChipItems = $derived(
    visibleSkillItems.map((skill) => ({ ...skill, allowed: skill.isAllowed })),
  );
  let agentTargetChipItems = $derived(
    visibleAgentTargetItems.map((target) => ({
      ...target,
      allowed: target.isAllowed,
    })),
  );
  // The memory tool from the catalog (if present), rendered as a display-only
  // first row: it follows the Memory setting and is never an allow-list toggle.
  let memoryToolItem = $derived(
    availableTools.find((tool) => tool.name === MEMORY_TOOL_NAME) ?? null,
  );
  // The wildcard default (`["*"]`) means "everything, including future items".
  // The toggle list renders every item as on with no signal, so a note explains
  // that flipping any single toggle collapses the wildcard into a fixed list.
  let toolsAreWildcard = $derived(isWildcardAccess(formValues.allowed_tools));
  let skillsAreWildcard = $derived(isWildcardAccess(formValues.allowed_skills));
  let configuredAgentTargets = $derived(
    subagentAllowedAgents(formValues.tools),
  );
  let agentsAreWildcard = $derived(isWildcardAccess(configuredAgentTargets));
  let subagentToolEnabled = $derived(
    accessAllowsSubagent(formValues.allowed_tools),
  );
  let showAllModels = $state(false);
  let showAllFallbackModels = $state(false);
  let allModelOptions = $derived(
    selectModelOptions(formValues.model, inheritModelLabel('model')),
  );
  let allFallbackModelOptions = $derived(
    selectModelOptions(
      formValues.fallback_model,
      inheritModelLabel('fallback_model'),
    ),
  );
  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: formValues.model,
    }),
  );
  let fallbackModelOptions = $derived(
    filterModelSelectOptions(allFallbackModelOptions, {
      showAll: showAllFallbackModels,
      selectedModelValue: formValues.fallback_model,
    }),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
      translate: t,
    }),
  );
  let fallbackModelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllFallbackModels,
      hiddenCount: allFallbackModelOptions.length - fallbackModelOptions.length,
      translate: t,
    }),
  );
  let modelSelectValue = $derived(
    selectModelValue(formValues.model, modelOptions),
  );
  let fallbackModelSelectValue = $derived(
    selectModelValue(formValues.fallback_model, fallbackModelOptions),
  );
  let selectedModelReasoning = $derived(
    reasoningForModelValue(formValues.model, availableModels),
  );
  // A non-reasoning model has no effort to steer — the control is disabled.
  // Reasoning support is treated as enabled unless the catalog says ``false``
  // (an unknown/custom model stays editable).
  let effortDropdownDisabled = $derived(
    selectedModelReasoning?.supported === false,
  );
  let thinkingEffortOptions = $derived(
    effortOptionsForReasoning(selectedModelReasoning).map((option) => ({
      value: option,
      label: thinkingEffortLabel(option),
    })),
  );
  // The per-field effective value + winning source from the agent payload, so an
  // empty (inherit) field can describe what it inherits. Absent for the create
  // form (no persisted agent yet) — the create modal builds its own labels.
  let effectiveConfig = $derived(
    agent?.effective && typeof agent.effective === 'object'
      ? agent.effective
      : {},
  );
  let temperatureIsInherit = $derived(formValues.temperature === '');
  let memoryPromptOptions = $derived(
    AGENT_MEMORY_PROMPT_MODES.map((option) => ({
      value: option,
      label: memoryPromptLabel(option),
    })),
  );
  let projectDropdownOptions = $derived(buildProjectDropdownOptions());

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

    agentAutoSaveTimer = setTimeout(() => {
      agentAutoSaveTimer = null;
      void saveAgent(null, { source: 'auto' });
    }, AUTO_SAVE_DEBOUNCE_MS);

    return () => {
      clearAgentAutoSaveTimer();
    };
  });

  onDestroy(() => {
    destroyed = true;
    clearAgentAutoSaveTimer();
  });

  async function saveAgent(event = null, options = {}) {
    event?.preventDefault?.();

    const source = options.source ?? 'manual';
    if (source === 'manual') {
      clearAgentAutoSaveTimer();
    }

    if (isSaving || isDeleting || workspaceDecisionOpen) {
      return;
    }

    const result = normalizeAgentForm(formValues, {
      mode: formMode,
      initialValues:
        formMode === AGENT_FORM_MODE_EDIT ? editBaselineValues : null,
    });

    if (source === 'manual') {
      formErrors = result.errors;
      errorMessage = '';
    }

    if (!result.isValid) {
      if (source === 'manual') {
        errorMessage = t(
          'errors.validation',
          'Check the highlighted fields and try again.',
        );
      }
      return;
    }

    if (
      formMode === AGENT_FORM_MODE_EDIT &&
      !agentPayloadHasChanges(result.payload)
    ) {
      if (source === 'manual') {
        showAgentToast(t('common.alreadySaved', 'Already saved'));
      }
      return;
    }

    if (
      source === 'manual' &&
      formMode === AGENT_FORM_MODE_EDIT &&
      Object.hasOwn(result.payload, 'workspace') &&
      options.workspaceCopyChoice === undefined
    ) {
      workspaceDecisionOpen = true;
      return;
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
        if (updatedSelectedAgent) {
          showAgentToast(t('agents.updated', 'Agent updated.'));
        }
      }
    } catch (error) {
      if (
        saveMode === AGENT_FORM_MODE_CREATE ||
        (!destroyed && editorAgentId === saveAgentId)
      ) {
        errorMessage = viewErrorMessage(error, t('agents.saveError'));
      }
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
      destroyed
    ) {
      return false;
    }

    const result = normalizeAgentForm(formValues, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues: editBaselineValues,
    });

    return (
      result.isValid &&
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
    await saveAgent(null, { source: 'manual' });
  }

  function chooseWorkspaceCopy(copyFiles) {
    workspaceDecisionOpen = false;
    void saveAgent(null, {
      source: 'manual',
      workspaceCopyChoice: copyFiles,
    });
  }

  function cancelWorkspaceDecision() {
    workspaceDecisionOpen = false;
  }

  function buildProjectDropdownOptions() {
    const options = [
      {
        value: '',
        label: t('agents.form.noProject', 'No project'),
      },
      ...(Array.isArray(projectOptions) ? projectOptions : []),
    ];
    const selected = formValues.root_project_id;
    if (selected && !options.some((option) => option.value === selected)) {
      options.push({
        value: selected,
        label: t('agents.form.unavailableProject', 'Unavailable project'),
        secondaryLabel: selected,
        disabled: true,
      });
    }
    return options;
  }

  function clearAgentAutoSaveTimer() {
    if (!agentAutoSaveTimer) {
      return;
    }

    clearTimeout(agentAutoSaveTimer);
    agentAutoSaveTimer = null;
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
      allowed_tools: Array.isArray(values.allowed_tools)
        ? [...values.allowed_tools]
        : [],
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

  function updateAccessItem(fieldName, itemName, isAllowed) {
    if (fieldName === 'allowed_tools') {
      updateToolAccessItem(itemName, isAllowed);
      return;
    }

    if (fieldName === 'allowed_skills') {
      updateSkillAccessItem(itemName, isAllowed);
      return;
    }

    if (fieldName === 'allowed_agents') {
      updateAgentTargetAccessItem(itemName, isAllowed);
    }
  }

  function setAccessItems(fieldName, isAllowed) {
    if (fieldName === 'allowed_tools') {
      setAllowedTools(isAllowed ? [WILDCARD_ACCESS] : []);
      return;
    }

    if (fieldName === 'allowed_skills') {
      formValues.allowed_skills = isAllowed ? [WILDCARD_ACCESS] : [];
      return;
    }

    if (fieldName === 'allowed_agents') {
      formValues.tools = withSubagentAllowedAgents(
        formValues.tools,
        isAllowed ? [WILDCARD_ACCESS] : [],
      );
    }
  }

  function updateToolAccessItem(itemName, isAllowed) {
    const allToolNames = configurableTools().map((tool) => tool.name);

    if (allToolNames.length === 0) {
      setAllowedTools([]);
      return;
    }

    const currentItems = Array.isArray(formValues.allowed_tools)
      ? [...formValues.allowed_tools]
      : [];

    if (currentItems.includes(WILDCARD_ACCESS)) {
      if (isAllowed) {
        setAllowedTools([WILDCARD_ACCESS]);
        return;
      }

      setAllowedTools(allToolNames.filter((name) => name !== itemName));
      return;
    }

    const nextItems = currentItems.filter((item) =>
      allToolNames.includes(item),
    );
    const existingIndex = nextItems.indexOf(itemName);

    if (isAllowed && existingIndex === -1) {
      nextItems.push(itemName);
    }

    if (!isAllowed && existingIndex !== -1) {
      nextItems.splice(existingIndex, 1);
    }

    setAllowedTools(
      allToolNames.every((name) => nextItems.includes(name))
        ? [WILDCARD_ACCESS]
        : nextItems,
    );
  }

  function setAllowedTools(items) {
    formValues.allowed_tools = items;
  }

  function accessAllowsSubagent(items) {
    return (
      isWildcardAccess(items) ||
      (Array.isArray(items) &&
        items.some((item) => ['subagent', 'subagent_result'].includes(item)))
    );
  }

  function cloneTools(tools) {
    return tools && typeof tools === 'object'
      ? JSON.parse(JSON.stringify(tools))
      : {};
  }

  function isWildcardAccess(items) {
    return Array.isArray(items) && items.includes(WILDCARD_ACCESS);
  }

  function toolAccessItems() {
    const currentItems = Array.isArray(formValues.allowed_tools)
      ? formValues.allowed_tools
      : [];
    const hasWildcard = currentItems.includes(WILDCARD_ACCESS);
    const allowedItems = hasWildcard ? [] : currentItems;

    return configurableTools().map((tool) => ({
      ...tool,
      isAllowed: hasWildcard || allowedItems.includes(tool.name),
    }));
  }

  function configurableTools() {
    return availableTools.filter((tool) => tool.name !== MEMORY_TOOL_NAME);
  }

  function skillAccessItems() {
    const currentItems = Array.isArray(formValues.allowed_skills)
      ? formValues.allowed_skills
      : [];
    const hasWildcard = currentItems.includes(WILDCARD_ACCESS);
    const allowedItems = hasWildcard ? [] : currentItems;

    return availableSkills.map((skill) => ({
      ...skill,
      warnings: Array.isArray(skill.warnings) ? skill.warnings : [],
      isAllowed: hasWildcard || allowedItems.includes(skill.name),
    }));
  }

  function agentTargetAccessItems() {
    const currentItems = configuredAgentTargets;
    const hasWildcard = currentItems.includes(WILDCARD_ACCESS);
    const catalog = Array.isArray(availableAgentTargets)
      ? availableAgentTargets.filter(
          (target) =>
            target.kind !== 'identity' || target.name !== formValues.id,
        )
      : [];
    const knownNames = new Set(catalog.map((target) => target.name));
    const missingTargets = hasWildcard
      ? []
      : currentItems
          .filter((name) => !knownNames.has(name))
          .map((name) => ({ name, unavailable: true }));

    return [...catalog, ...missingTargets].map((target) => ({
      ...target,
      description: agentTargetDescription(target),
      isAllowed: hasWildcard || currentItems.includes(target.name),
    }));
  }

  function agentTargetDescription(target) {
    if (target.unavailable) {
      return t(
        'agents.access.unavailableAgentTarget',
        'This configured target is not present in the current Identity Agent or Project Team catalogs.',
      );
    }
    if (target.kind === 'project') {
      return t(
        'agents.access.projectAgentTarget',
        'Project Agent · {agent} · {project}',
        {
          agent: target.displayName || target.name,
          project: target.projectName || target.projectId,
        },
      );
    }
    return t('agents.access.identityAgentTarget', 'Identity Agent · {agent}', {
      agent: target.displayName || target.name,
    });
  }

  function updateAgentTargetAccessItem(itemName, isAllowed) {
    const allTargetNames = agentTargetAccessItems().map(
      (target) => target.name,
    );
    if (allTargetNames.length === 0) {
      formValues.tools = withSubagentAllowedAgents(formValues.tools, []);
      return;
    }

    const currentItems = [...configuredAgentTargets];
    if (currentItems.includes(WILDCARD_ACCESS)) {
      formValues.tools = withSubagentAllowedAgents(
        formValues.tools,
        isAllowed
          ? [WILDCARD_ACCESS]
          : allTargetNames.filter((name) => name !== itemName),
      );
      return;
    }

    const nextItems = currentItems.filter((item) =>
      allTargetNames.includes(item),
    );
    const existingIndex = nextItems.indexOf(itemName);
    if (isAllowed && existingIndex === -1) {
      nextItems.push(itemName);
    } else if (!isAllowed && existingIndex !== -1) {
      nextItems.splice(existingIndex, 1);
    }
    formValues.tools = withSubagentAllowedAgents(
      formValues.tools,
      allTargetNames.every((name) => nextItems.includes(name))
        ? [WILDCARD_ACCESS]
        : nextItems,
    );
  }

  function updateSkillAccessItem(itemName, isAllowed) {
    const allSkillNames = availableSkills.map((skill) => skill.name);

    if (allSkillNames.length === 0) {
      formValues.allowed_skills = [];
      return;
    }

    const currentItems = Array.isArray(formValues.allowed_skills)
      ? [...formValues.allowed_skills]
      : [];

    if (currentItems.includes(WILDCARD_ACCESS)) {
      if (isAllowed) {
        formValues.allowed_skills = [WILDCARD_ACCESS];
        return;
      }

      formValues.allowed_skills = allSkillNames.filter(
        (name) => name !== itemName,
      );
      return;
    }

    const nextItems = currentItems.filter((item) =>
      allSkillNames.includes(item),
    );
    const existingIndex = nextItems.indexOf(itemName);

    if (isAllowed && existingIndex === -1) {
      nextItems.push(itemName);
    }

    if (!isAllowed && existingIndex !== -1) {
      nextItems.splice(existingIndex, 1);
    }

    formValues.allowed_skills = allSkillNames.every((name) =>
      nextItems.includes(name),
    )
      ? [WILDCARD_ACCESS]
      : nextItems;
  }

  function selectModelOptions(selectedModelValue, emptyLabel) {
    return buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue,
      emptyLabel,
      translate: t,
    });
  }

  function updateModelSelection(modelFieldName, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    formValues[modelFieldName] = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );
  }

  function thinkingEffortLabel(option) {
    // The empty option is the inherit state: describe what it inherits. A global
    // default value → "Inherited: <value> (global default)"; nothing set anywhere
    // → the provider default falls through.
    if (option === '') {
      if (inheritSource('thinking_effort') === 'global_default') {
        return t('inherit.option', 'Inherited: {value} (global default)', {
          value: inheritDisplayValue('thinking_effort'),
        });
      }
      return t('inherit.optionProviderDefault', 'Inherit (provider default)');
    }

    return t(`agents.form.thinkingEffortOption.${option}`, option);
  }

  // The empty-option label for the model / fallback-model select. Uses that
  // field's effective source: a global default fills the value; a fully
  // unconfigured model shows "Inherit (not configured)".
  function inheritModelLabel(fieldName) {
    if (inheritSource(fieldName) === 'global_default') {
      return t('inherit.option', 'Inherited: {value} (global default)', {
        value: inheritDisplayValue(fieldName),
      });
    }
    return t('inherit.optionNotConfigured', 'Inherit (not configured)');
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

  function clearTemperature() {
    formValues.temperature = '';
  }

  function setOwnCompactionPolicy(enabled) {
    formValues.compaction_policy = enabled
      ? structuredClone(agent?.effective_compaction_policy ?? {})
      : null;
  }

  function memoryToolRowText() {
    return formValues.memory_prompt_mode === 'off'
      ? t(
          'agents.tools.memoryFollowsOff',
          'Follows the Memory setting — currently unavailable (Memory is off).',
        )
      : t(
          'agents.tools.memoryFollowsActive',
          'Follows the Memory setting — currently available.',
        );
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

  function memoryPromptLabel(option) {
    return t(`agents.form.memoryPromptModeOption.${option}`, option);
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

    return timestampFormatter.format(new Date(parsedValue));
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

{#snippet globalDefaultsLink()}
  {#if formMode === AGENT_FORM_MODE_EDIT}
    <Button
      variant="tertiary"
      class="agents-view__inherit-link"
      onClick={navigateToAgentDefaults}
    >
      {t('inherit.editGlobalDefaults', 'Edit global defaults')}
    </Button>
  {/if}
{/snippet}

<form class="agent-detail-pane" onsubmit={saveAgent}>
  <div class="agent-detail-scroll">
    <div class="detail-top">
      <div>
        <div class="detail-heading">
          {formMode === AGENT_FORM_MODE_CREATE
            ? t('agents.create', 'Create agent')
            : agent?.name || formValues.name || agent?.id}
        </div>
        <div class="detail-sub">{detailSubtitle}</div>
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

    {#if errorMessage}
      <Banner variant="error" role="alert">
        {errorMessage}
      </Banner>
    {/if}

    <div class="detail-group">
      <div class="detail-group-title">
        {t('agents.detail.identity', 'Identity')}
      </div>
      <div class="detail-fields">
        <FormField
          controlId="agent-id"
          label={t('agents.form.id', 'Agent ID')}
          required
          help={t(
            'agents.form.idHelp',
            'Agent IDs are immutable after creation.',
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
        </FormField>

        <FormField
          controlId="agent-name"
          label={t('agents.form.name', 'Name')}
          error={formErrors.name ? fieldError('name') : ''}
        >
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              invalid={field.invalid}
              aria-describedby={field.describedBy}
              value={formValues.name}
              onInput={(next) => (formValues.name = next)}
            />
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

        {#if formMode === AGENT_FORM_MODE_EDIT}
          <FormField
            controlId="agent-project"
            full
            label={t('agents.form.project', 'Project')}
            help={projectCatalogError
              ? t(
                  'agents.form.projectUnavailableHelp',
                  'The saved selection is preserved. Project editing is unavailable until the catalog reloads.',
                )
              : t(
                  'agents.form.projectHelp',
                  'Where relative file and shell work runs. Workspace remains the identity and memory home.',
                )}
          >
            <Dropdown
              id="agent-project"
              value={formValues.root_project_id ?? ''}
              options={projectDropdownOptions}
              disabled={Boolean(projectCatalogError)}
              ariaLabel={t('agents.form.project', 'Project')}
              triggerClass="agents-view__dropdown"
              onValueChange={(selectedValue) => {
                formValues.root_project_id = selectedValue || null;
              }}
            />
          </FormField>
        {/if}
      </div>
    </div>

    <div class="detail-group agents-view__model-group">
      <div class="detail-group-title">
        {t('agents.detail.model', 'Model')}
      </div>
      <div class="detail-fields agents-view__model-fields">
        <FormField
          controlId="agent-model"
          full
          label={t('agents.form.model', 'Model')}
        >
          <SearchableDropdown
            id="agent-model"
            value={modelSelectValue}
            options={modelOptions}
            placeholder={inheritModelLabel('model')}
            searchPlaceholder={t(
              'agents.form.modelSearchPlaceholder',
              'Filter models…',
            )}
            emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
            ariaLabel={t('agents.form.model', 'Model')}
            triggerClass="agents-view__dropdown"
            panelClass="agents-view__search-panel"
            footerActionLabel={modelFilterFooter}
            onFooterAction={() => (showAllModels = !showAllModels)}
            onOpenChange={onModelDropdownOpenChange}
            onValueChange={(selectedValue) =>
              updateModelSelection('model', selectedValue)}
          />
          {#snippet actions()}{@render globalDefaultsLink()}{/snippet}
        </FormField>

        <FormField controlId="agent-fallback-model">
          {#snippet labelContent()}
            {t('agents.form.fallbackModel', 'Fallback model')}
            <InfoHint
              text={t(
                'agents.form.fallbackModelHelp',
                'Used automatically when the primary model fails or is unavailable.',
              )}
            />
          {/snippet}
          <SearchableDropdown
            id="agent-fallback-model"
            value={fallbackModelSelectValue}
            options={fallbackModelOptions}
            placeholder={inheritModelLabel('fallback_model')}
            searchPlaceholder={t(
              'agents.form.modelSearchPlaceholder',
              'Filter models…',
            )}
            emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
            ariaLabel={t('agents.form.fallbackModel', 'Fallback model')}
            triggerClass="agents-view__dropdown"
            panelClass="agents-view__search-panel"
            footerActionLabel={fallbackModelFilterFooter}
            onFooterAction={() =>
              (showAllFallbackModels = !showAllFallbackModels)}
            onOpenChange={onModelDropdownOpenChange}
            onValueChange={(selectedValue) =>
              updateModelSelection('fallback_model', selectedValue)}
          />
          {#snippet actions()}{@render globalDefaultsLink()}{/snippet}
        </FormField>

        <FormField
          controlId="agent-thinking-effort"
          class="agents-view__thinking-field"
          help={effortDropdownDisabled
            ? t(
                'agents.form.thinkingEffortUnsupported',
                'This model does not support reasoning.',
              )
            : ''}
        >
          {#snippet labelContent()}
            {t('agents.form.thinkingEffort', 'Thinking effort')}
            <InfoHint
              text={t(
                'agents.form.thinkingEffortHelp',
                'How much internal reasoning the model may spend before answering. Leave at — for the default.',
              )}
            />
          {/snippet}
          <Dropdown
            id="agent-thinking-effort"
            value={formValues.thinking_effort}
            options={thinkingEffortOptions}
            disabled={effortDropdownDisabled}
            ariaLabel={t('agents.form.thinkingEffort', 'Thinking effort')}
            triggerClass="agents-view__dropdown"
            listClass="agents-view__thinking-list"
            onValueChange={(selectedValue) => {
              formValues.thinking_effort = selectedValue;
            }}
          />
          {#snippet actions()}
            {#if formValues.thinking_effort === ''}
              {@render globalDefaultsLink()}
            {/if}
          {/snippet}
        </FormField>

        <FormField
          controlId="agent-temperature"
          help={temperatureIsInherit
            ? inheritSource('temperature') === 'global_default'
              ? t('inherit.hint', 'Inherited: {value} (global default)', {
                  value: inheritDisplayValue('temperature'),
                })
              : t(
                  'inherit.hintProviderDefault',
                  'Provider default — nothing is set here or in the global defaults.',
                )
            : ''}
          error={formErrors.temperature ? fieldError('temperature') : ''}
        >
          {#snippet labelContent()}
            {t('agents.form.temperature', 'Temperature')}
            <InfoHint
              text={t(
                'agents.form.temperatureHelp',
                'Sampling randomness, typically 0–2. Leave empty to use the default.',
              )}
            />
          {/snippet}
          {#snippet children(field)}
            <div class="agents-view__temperature-input">
              <TextField
                id={field.controlId}
                inputmode="decimal"
                invalid={field.invalid}
                aria-describedby={field.describedBy}
                value={formValues.temperature}
                onInput={(next) => (formValues.temperature = next)}
              />
              {#if !temperatureIsInherit}
                <Button
                  variant="tertiary"
                  class="agents-view__reset-inherit"
                  tooltip={t(
                    'inherit.resetToInherit',
                    'Reset to inherited value',
                  )}
                  ariaLabel={t(
                    'inherit.resetToInherit',
                    'Reset to inherited value',
                  )}
                  onClick={clearTemperature}
                >
                  {EMPTY_VALUE}
                </Button>
              {/if}
            </div>
          {/snippet}
          {#snippet actions()}
            {#if temperatureIsInherit}
              {@render globalDefaultsLink()}
            {/if}
          {/snippet}
        </FormField>
      </div>
    </div>

    <div class="detail-group agents-view__compaction-group">
      <div class="detail-group-title">
        {t('agents.detail.compaction', 'Compaction Policy')}
      </div>
      <div class="agents-view__prompt-toggle-row">
        <div>
          <div class="agents-view__prompt-toggle-label">
            {formValues.compaction_policy
              ? t('compaction.scope.agentOwn', 'Use an Agent Policy')
              : t(
                  'compaction.scope.inheritGlobal',
                  'Inherit the global Policy live',
                )}
          </div>
          <div class="agents-view__prompt-toggle-desc">
            {t(
              'compaction.scope.agentDescription',
              'Inherited changes apply to this Agent’s existing Sessions unless a Session has its own override.',
            )}
          </div>
        </div>
        <Toggle
          checked={formValues.compaction_policy !== null}
          ariaLabel={t('compaction.scope.agentOwn', 'Use an Agent Policy')}
          onChange={setOwnCompactionPolicy}
        />
      </div>
      {#if formValues.compaction_policy}
        <CompactionPolicyEditor
          value={formValues.compaction_policy}
          onChange={(next) => (formValues.compaction_policy = next)}
          idPrefix="agent-compaction"
        />
      {/if}
    </div>

    <div class="detail-group agents-view__prompt-group">
      <div class="detail-group-title">
        {t('agents.detail.systemPrompt', 'System Prompt')}
      </div>
      <div class="agents-view__prompt-toggle-row">
        <span class="agents-view__prompt-toggle-label">
          {t('agents.form.customSystemPrompt', 'Custom system prompt')}
          <InfoHint
            text={t(
              'agents.form.customPromptHelp',
              'Gives this agent its own editable copy of the system prompt. Edit it in the System Prompt tab by selecting this agent as the scope. Turning this off keeps the customized blocks but stops using them.',
            )}
          />
        </span>
        <div class="agents-view__prompt-toggle-controls">
          {#if formMode === AGENT_FORM_MODE_EDIT && formValues.custom_system_prompt_enabled}
            <Button
              variant="tertiary"
              class="agents-view__inherit-link"
              onClick={navigateToAgentPrompt}
            >
              {t('agents.form.editAgentPrompt', "Edit this agent's prompt")}
            </Button>
          {/if}
          <Toggle
            size="sm"
            class="agents-view__prompt-toggle"
            checked={formValues.custom_system_prompt_enabled}
            ariaLabel={t(
              'agents.form.customSystemPrompt',
              'Custom system prompt',
            )}
            disabled={formMode === AGENT_FORM_MODE_CREATE}
            onChange={(next) => {
              void handleCustomPromptToggle(next);
            }}
          />
        </div>
      </div>
    </div>

    <div class="detail-group agents-view__memory-group">
      <div class="detail-group-title">
        {t('agents.detail.memory', 'Memory')}
      </div>
      <div class="agents-view__prompt-memory-row">
        <span class="agents-view__prompt-toggle-label">
          {t('agents.form.memoryPromptMode', 'Memory')}
          <InfoHint
            text={t(
              'agents.form.memoryModeHelp',
              'Which memory files are pinned into the System Prompt. The memory tool follows this setting — it is available to the agent unless this is off.',
            )}
          />
        </span>
        <Dropdown
          id="agent-memory-prompt-mode"
          value={formValues.memory_prompt_mode}
          options={memoryPromptOptions}
          ariaLabel={t('agents.form.memoryPromptMode', 'Memory')}
          triggerClass="agents-view__memory-dropdown"
          listClass="agents-view__memory-list"
          onValueChange={(selectedValue) => {
            formValues.memory_prompt_mode = selectedValue;
          }}
        />
      </div>
    </div>

    <div class="detail-group">
      <div class="detail-group-title">
        {t('agents.detail.access', 'Access')}
      </div>

      <div class="tl-section">
        <div class="tl-section-header">
          <span class="tl-section-label">
            {t('agents.form.allowedTools', 'Allowed tools')}
          </span>
        </div>
        <ToggleChipList
          items={toolChipItems}
          note={toolsAreWildcard && visibleToolItems.length > 0
            ? t(
                'agents.form.wildcardNote',
                'Currently all are allowed, including ones added in the future. Turning any single item off switches to a fixed list.',
              )
            : ''}
          ariaToggleLabel={(name) =>
            t('agents.access.toggleTool', 'Toggle tool {name}', { name })}
          onToggle={(name, next) =>
            updateAccessItem('allowed_tools', name, next)}
          onSetAll={(next) => setAccessItems('allowed_tools', next)}
          onOpenExtensions={navigateToExtensions}
        />
      </div>

      {#if subagentToolEnabled}
        <div class="tl-section">
          <div class="tl-section-header">
            <span class="tl-section-label">
              {t('agents.form.subagentSettings', 'Sub-Agent settings')}
              <InfoHint
                text={t(
                  'agents.form.allowedAgentsHelp',
                  'Additional targets for subagent and subagent_result. The calling Agent is always available by omitting agent_id and is not listed here. Project Agents use agent@project ids. Rooting does not narrow this permission.',
                )}
              />
            </span>
          </div>
          <ToggleChipList
            items={agentTargetChipItems}
            emptyLabel={t(
              'agents.access.noAgentTargets',
              'No additional Agent targets are available.',
            )}
            note={agentsAreWildcard && visibleAgentTargetItems.length > 0
              ? t(
                  'agents.form.agentWildcardNote',
                  'Additional Agents: all other Identity Agents and all Agents on every registered Project, including ones added later. The calling Agent remains implicit. Rooting does not narrow this.',
                )
              : t(
                  'agents.form.agentAddressNote',
                  'Additional Agents use bare Identity ids or agent@project ids. The calling Agent remains implicit. Rooting does not change this list.',
                )}
            ariaToggleLabel={(name) =>
              t('agents.access.toggleAgent', 'Toggle agent {name}', { name })}
            onToggle={(name, next) =>
              updateAccessItem('allowed_agents', name, next)}
            onSetAll={(next) => setAccessItems('allowed_agents', next)}
          />
          {#if agentTargetCatalogError}
            <p class="agents-view__placeholder-row" role="status">
              {agentTargetCatalogError}
            </p>
          {/if}
        </div>
      {/if}

      <div class="tl-section">
        <div class="tl-section-header">
          <span class="tl-section-label">
            {t('agents.form.allowedSkills', 'Allowed skills')}
          </span>
        </div>
        <ToggleChipList
          items={skillChipItems}
          emptyLabel={t(
            'agents.access.noSkills',
            'No loadable skills are available.',
          )}
          note={skillsAreWildcard && visibleSkillItems.length > 0
            ? t(
                'agents.form.wildcardNote',
                'Currently all are allowed, including ones added in the future. Turning any single item off switches to a fixed list.',
              )
            : ''}
          ariaToggleLabel={(name) =>
            t('agents.access.toggleSkill', 'Toggle skill {name}', { name })}
          onToggle={(name, next) =>
            updateAccessItem('allowed_skills', name, next)}
          onSetAll={(next) => setAccessItems('allowed_skills', next)}
        />
        {#if invalidSkills.length > 0}
          <div class="agents-view__invalid-skills">
            <div class="agents-view__invalid-skills-title">
              {t('agents.access.invalidSkillsTitle', 'Unavailable skills')}
            </div>
            <div class="agents-view__invalid-skills-list">
              {#each invalidSkills as item (item.path || item.name)}
                <div class="agents-view__invalid-skill">
                  <div class="agents-view__access-copy">
                    <span class="tl-item-name">
                      {item.name ||
                        t('agents.access.unknownSkillName', 'Unknown skill')}
                    </span>
                    {#if item.path}
                      <span class="agents-view__invalid-skill-path">
                        {item.path}
                      </span>
                    {/if}
                    {#if Array.isArray(item.warnings) && item.warnings.length > 0}
                      <div class="agents-view__skill-warnings">
                        <span class="agents-view__warning-label">
                          {t('agents.access.skillWarnings', 'Warnings')}
                        </span>
                        <ul>
                          {#each item.warnings as warning, index (`${item.path || item.name}-warning-${index}`)}
                            <li>{warning}</li>
                          {/each}
                        </ul>
                      </div>
                    {/if}
                  </div>
                  <StatusChip variant="warn">
                    {t('agents.access.notLoadable', 'not loadable')}
                  </StatusChip>
                </div>
              {/each}
            </div>
          </div>
        {/if}
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

    {#if formMode === AGENT_FORM_MODE_EDIT && !canDeleteSelectedAgent}
      <p class="agents-view__placeholder-row">
        {t(
          'agents.deleteDisabledMinimum',
          'The last remaining agent cannot be deleted.',
        )}
      </p>
    {/if}

    <div class="agent-detail-footer">
      <Button variant="secondary" type="submit" disabled={isSaving}>
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
