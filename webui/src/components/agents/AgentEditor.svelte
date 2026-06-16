<script>
  import { onDestroy, untrack } from 'svelte';

  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import StatusChip from '../ui/StatusChip.svelte';
  import TextField from '../ui/TextField.svelte';
  import Toggle from '../ui/Toggle.svelte';
  import { rpc } from '$lib/api.js';
  import {
    AGENT_MEMORY_PROMPT_MODES,
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    MEMORY_TOOL_NAME,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';

  const EMPTY_VALUE = '—';
  const AUTO_SAVE_DEBOUNCE_MS = 800;
  const timestampFormatter = new Intl.DateTimeFormat(activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
  const WILDCARD_ACCESS = '*';
  const THINKING_EFFORT_OPTIONS = Object.freeze([
    '',
    'none',
    'minimal',
    'low',
    'medium',
    'high',
    'xhigh',
    'max',
  ]);

  let {
    agent = null,
    agentsCount = 0,
    availableModels = [],
    availableConnections = [],
    availableTools = [],
    availableSkills = [],
    invalidSkills = [],
    loadError = '',
    onAgentUpdated = () => {},
    onAgentCreated = async () => {},
    onAgentDeleted = async () => {},
    onToast = () => {},
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

  let canDeleteSelectedAgent = $derived(Boolean(agent) && agentsCount > 1);
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
  let modelOptions = $derived(
    selectModelOptions(
      formValues.model,
      t('agents.form.modelPlaceholder', 'Default (no model selected)'),
    ),
  );
  let fallbackModelOptions = $derived(
    selectModelOptions(
      formValues.fallback_model,
      t('agents.form.fallbackModelPlaceholder', 'None'),
    ),
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
  let memoryPromptOptions = $derived(
    AGENT_MEMORY_PROMPT_MODES.map((option) => ({
      value: option,
      label: memoryPromptLabel(option),
    })),
  );

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

    if (isSaving || isDeleting) {
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

    isSaving = true;
    const saveMode = formMode;
    const saveAgentId = result.payload.id;
    const draftValues = cloneAgentFormValues(formValues);
    errorMessage = '';

    try {
      const method =
        saveMode === AGENT_FORM_MODE_CREATE ? 'agent.create' : 'agent.update';
      const savedAgent = await rpc(method, result.payload);
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
      agentPayloadHasChanges(result.payload)
    );
  }

  function agentPayloadHasChanges(payload) {
    return Object.keys(payload).some((fieldName) => fieldName !== 'id');
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
      await rpc('agent.delete', { id: agent.id });
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
    }
  }

  function setAccessItems(fieldName, isAllowed) {
    if (fieldName === 'allowed_tools') {
      formValues.allowed_tools = isAllowed ? [WILDCARD_ACCESS] : [];
      return;
    }

    if (fieldName === 'allowed_skills') {
      formValues.allowed_skills = isAllowed ? [WILDCARD_ACCESS] : [];
    }
  }

  function updateToolAccessItem(itemName, isAllowed) {
    const allToolNames = configurableTools().map((tool) => tool.name);

    if (allToolNames.length === 0) {
      formValues.allowed_tools = [];
      return;
    }

    const currentItems = Array.isArray(formValues.allowed_tools)
      ? [...formValues.allowed_tools]
      : [];

    if (currentItems.includes(WILDCARD_ACCESS)) {
      if (isAllowed) {
        formValues.allowed_tools = [WILDCARD_ACCESS];
        return;
      }

      formValues.allowed_tools = allToolNames.filter(
        (name) => name !== itemName,
      );
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

    formValues.allowed_tools = allToolNames.every((name) =>
      nextItems.includes(name),
    )
      ? [WILDCARD_ACCESS]
      : nextItems;
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
    if (option === '') {
      return t('agents.form.thinkingEffortDefault', EMPTY_VALUE);
    }

    return t(`agents.form.thinkingEffortOption.${option}`, option);
  }

  function reasoningForModelValue(modelValue, models) {
    const { model } = parseModelSelectionValue(modelValue);
    if (!model) {
      return null;
    }
    const match = models.find((candidate) => candidate.id === model);
    return match?.capabilities?.reasoning ?? null;
  }

  function effortOptionsForReasoning(reasoning) {
    // No catalog reasoning info (unknown/custom model) or no feed ladder: keep
    // the full ladder — the adapter applies a provider-specific floor the UI
    // cannot see, so it must not hide options that may be valid.
    const levels = Array.isArray(reasoning?.levels) ? reasoning.levels : [];
    if (levels.length === 0) {
      return THINKING_EFFORT_OPTIONS;
    }
    // A model with a published ladder shows only its possible efforts: the
    // default (provider default) and "none" (turn reasoning off) always apply;
    // the remaining options are exactly the model's levels, kept in canonical
    // order so the dropdown reads consistently.
    const allowed = new Set(['', 'none', ...levels]);
    return THINKING_EFFORT_OPTIONS.filter((option) => allowed.has(option));
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
          <Button
            variant="danger"
            disabled={isDeleting || !canDeleteSelectedAgent}
            title={!canDeleteSelectedAgent
              ? t(
                  'agents.deleteDisabledMinimum',
                  'The last remaining agent cannot be deleted.',
                )
              : t('agents.delete', 'Delete agent')}
            onClick={deleteSelectedAgent}
          >
            {isDeleting
              ? t('common.loading', 'Loading…')
              : t('agents.delete', 'Delete agent')}
          </Button>
        {/if}
      </div>
    </div>

    {#if errorMessage}
      <p class="agents-view__notice agents-view__notice--error" role="alert">
        {errorMessage}
      </p>
    {/if}

    <div class="detail-group">
      <div class="detail-group-title">
        {t('agents.detail.identity', 'Identity')}
      </div>
      <div class="detail-fields">
        <label class="f">
          <span class="f-label">{t('agents.form.id', 'Agent ID')}</span>
          <TextField
            invalid={Boolean(formErrors.id)}
            value={formValues.id}
            onInput={(next) => (formValues.id = next)}
            disabled={formMode === AGENT_FORM_MODE_EDIT}
            aria-describedby="agent-id-help agent-id-error"
          />
          <small id="agent-id-help" class="agents-view__field-help">
            {t('agents.form.idHelp', 'Agent IDs are immutable after creation.')}
          </small>
          {#if formErrors.id}
            <small id="agent-id-error" class="agents-view__field-error">
              {fieldError('id')}
            </small>
          {/if}
        </label>

        <label class="f">
          <span class="f-label">{t('agents.form.name', 'Name')}</span>
          <TextField
            invalid={Boolean(formErrors.name)}
            value={formValues.name}
            onInput={(next) => (formValues.name = next)}
          />
          {#if formErrors.name}
            <small class="agents-view__field-error">
              {fieldError('name')}
            </small>
          {/if}
        </label>

        <label class="f wide">
          <span class="f-label">
            {t('agents.form.workspace', 'Workspace')}
          </span>
          <TextField
            id="agent-workspace"
            class="mono"
            invalid={Boolean(formErrors.workspace)}
            value={formValues.workspace}
            onInput={(next) => (formValues.workspace = next)}
            disabled={formMode === AGENT_FORM_MODE_CREATE}
            aria-describedby="agent-workspace-help agent-workspace-error"
          />
          <small id="agent-workspace-help" class="agents-view__field-help">
            {formMode === AGENT_FORM_MODE_CREATE
              ? t(
                  'agents.form.workspaceAssignedByServer',
                  'Workspace is assigned by the server when the agent is created.',
                )
              : t(
                  'agents.form.workspaceEditableHelp',
                  "Workspace path used by this agent's tools.",
                )}
          </small>
          {#if formErrors.workspace}
            <small id="agent-workspace-error" class="agents-view__field-error">
              {fieldError('workspace')}
            </small>
          {/if}
        </label>
      </div>
    </div>

    <div class="detail-group agents-view__model-group">
      <div class="detail-group-title">
        {t('agents.detail.model', 'Model')}
      </div>
      <div class="detail-fields agents-view__model-fields">
        <label class="f wide">
          <span class="f-label">{t('agents.form.model', 'Model')}</span>
          <SearchableDropdown
            id="agent-model"
            value={modelSelectValue}
            options={modelOptions}
            placeholder={t(
              'agents.form.modelPlaceholder',
              'Default (no model selected)',
            )}
            searchPlaceholder={t(
              'agents.form.modelSearchPlaceholder',
              'Filter models…',
            )}
            emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
            ariaLabel={t('agents.form.model', 'Model')}
            triggerClass="agents-view__dropdown"
            panelClass="agents-view__search-panel"
            onValueChange={(selectedValue) =>
              updateModelSelection('model', selectedValue)}
          />
        </label>

        <label class="f">
          <span class="f-label">
            {t('agents.form.fallbackModel', 'Fallback model')}
          </span>
          <SearchableDropdown
            id="agent-fallback-model"
            value={fallbackModelSelectValue}
            options={fallbackModelOptions}
            placeholder={t('agents.form.fallbackModelPlaceholder', 'None')}
            searchPlaceholder={t(
              'agents.form.modelSearchPlaceholder',
              'Filter models…',
            )}
            emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
            ariaLabel={t('agents.form.fallbackModel', 'Fallback model')}
            triggerClass="agents-view__dropdown"
            panelClass="agents-view__search-panel"
            onValueChange={(selectedValue) =>
              updateModelSelection('fallback_model', selectedValue)}
          />
        </label>

        <label class="f agents-view__thinking-field">
          <span class="f-label">
            {t('agents.form.thinkingEffort', 'Thinking effort')}
          </span>
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
          {#if effortDropdownDisabled}
            <small
              class="agents-view__field-help"
              data-testid="thinking-effort-disabled-hint"
            >
              {t(
                'agents.form.thinkingEffortUnsupported',
                'This model does not support reasoning.',
              )}
            </small>
          {/if}
        </label>

        <label class="f">
          <span class="f-label">
            {t('agents.form.temperature', 'Temperature')}
          </span>
          <TextField
            inputmode="decimal"
            invalid={Boolean(formErrors.temperature)}
            value={formValues.temperature}
            onInput={(next) => (formValues.temperature = next)}
          />
          {#if formErrors.temperature}
            <small class="agents-view__field-error">
              {fieldError('temperature')}
            </small>
          {/if}
        </label>
      </div>
    </div>

    <div class="detail-group agents-view__prompt-group">
      <div class="detail-group-title">
        {t('agents.detail.systemPrompt', 'System Prompt')}
      </div>
      <div class="agents-view__prompt-toggle-row">
        <span class="agents-view__prompt-toggle-label">
          {t('agents.form.customSystemPrompt', 'Custom system prompt')}
        </span>
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
            formValues.custom_system_prompt_enabled = next;
          }}
        />
      </div>
      <div class="agents-view__prompt-memory-row">
        <span class="agents-view__prompt-toggle-label">
          {t('agents.form.memoryPromptMode', 'Memory')}
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
          <div class="tl-actions">
            <Button
              variant="tertiary"
              disabled={visibleToolItems.length === 0}
              onClick={() => setAccessItems('allowed_tools', true)}
            >
              {t('agents.access.allOn', 'all on')}
            </Button>
            <Button
              variant="tertiary"
              disabled={visibleToolItems.length === 0}
              onClick={() => setAccessItems('allowed_tools', false)}
            >
              {t('agents.access.allOff', 'all off')}
            </Button>
          </div>
        </div>
        {#if visibleToolItems.length > 0}
          <div class="tl-items">
            {#each visibleToolItems as item (item.name)}
              <div class="tl-item">
                <div class="agents-view__access-copy">
                  <span class="tl-item-name">{item.name}</span>
                  {#if item.description}
                    <span class="agents-view__access-description">
                      {t('agents.access.descriptionLabel', '{description}', {
                        description: item.description,
                      })}
                    </span>
                  {/if}
                </div>
                <Toggle
                  size="sm"
                  checked={item.isAllowed}
                  ariaLabel={t(
                    'agents.access.toggleTool',
                    'Toggle tool {name}',
                    { name: item.name },
                  )}
                  disabled={item.isWildcard}
                  onChange={(next) =>
                    updateAccessItem('allowed_tools', item.name, next)}
                />
              </div>
            {/each}
          </div>
        {/if}
      </div>

      <div class="tl-section">
        <div class="tl-section-header">
          <span class="tl-section-label">
            {t('agents.form.allowedSkills', 'Allowed skills')}
          </span>
          <div class="tl-actions">
            <Button
              variant="tertiary"
              disabled={visibleSkillItems.length === 0}
              onClick={() => setAccessItems('allowed_skills', true)}
            >
              {t('agents.access.allOn', 'all on')}
            </Button>
            <Button
              variant="tertiary"
              disabled={visibleSkillItems.length === 0}
              onClick={() => setAccessItems('allowed_skills', false)}
            >
              {t('agents.access.allOff', 'all off')}
            </Button>
          </div>
        </div>
        {#if visibleSkillItems.length > 0}
          <div class="tl-items">
            {#each visibleSkillItems as item (item.name)}
              <div class="tl-item">
                <div class="agents-view__access-copy">
                  <span class="tl-item-name">{item.name}</span>
                  {#if item.description}
                    <span class="agents-view__access-description">
                      {t('agents.access.descriptionLabel', '{description}', {
                        description: item.description,
                      })}
                    </span>
                  {/if}
                  {#if item.valid === false && item.warnings.length > 0}
                    <div class="agents-view__skill-warnings">
                      <span class="agents-view__warning-label">
                        {t('agents.access.skillWarnings', 'Warnings')}
                      </span>
                      <ul>
                        {#each item.warnings as warning, index (`${item.name}-warning-${index}`)}
                          <li>{warning}</li>
                        {/each}
                      </ul>
                    </div>
                  {/if}
                </div>
                <Toggle
                  size="sm"
                  checked={item.isAllowed}
                  ariaLabel={t(
                    'agents.access.toggleSkill',
                    'Toggle skill {name}',
                    { name: item.name },
                  )}
                  disabled={item.isWildcard}
                  onChange={(next) =>
                    updateAccessItem('allowed_skills', item.name, next)}
                />
              </div>
            {/each}
          </div>
        {:else}
          <p class="agents-view__placeholder-row">
            {t('agents.access.noSkills', 'No loadable skills are available.')}
          </p>
        {/if}
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
