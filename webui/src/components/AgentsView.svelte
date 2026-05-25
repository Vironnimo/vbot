<script>
  import { onMount } from 'svelte';

  import Dropdown from './Dropdown.svelte';
  import SearchableDropdown from './SearchableDropdown.svelte';
  import { rpc } from '$lib/api.js';
  import {
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';

  const EMPTY_VALUE = '—';
  const AUTO_SAVE_DEBOUNCE_MS = 800;
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
    sharedSelectedAgentId = '',
    onAgentsChanged,
    onAgentSelected,
  } = $props();

  let agents = $state([]);
  let selectedAgentId = $state('');
  let lastSharedSelectedAgentId = $state('');
  let formMode = $state(AGENT_FORM_MODE_CREATE);
  let formValues = $state(createAgentFormValues());
  let editBaselineValues = $state(createAgentFormValues());
  let formErrors = $state({});
  let isLoading = $state(false);
  let isSaving = $state(false);
  let isDeleting = $state(false);
  let errorMessage = $state('');
  let statusMessage = $state('');
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let availableTools = $state([]);
  let availableSkills = $state([]);
  let invalidSkills = $state([]);
  let modelSelectValue = $state('');
  let fallbackModelSelectValue = $state('');
  let agentAutoSaveTimer = null;

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
  let detailSubtitle = $derived(
    formMode === AGENT_FORM_MODE_CREATE
      ? t('agents.detail.newSubtitle', 'id assigned at creation')
      : t('agents.detail.idValue', 'id: {id}', {
          id: selectedAgent?.id ?? formValues.id,
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
  let thinkingEffortOptions = $derived(
    THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label: thinkingEffortLabel(option),
    })),
  );

  $effect(() => {
    modelSelectValue = selectModelValue(formValues.model, modelOptions);
    fallbackModelSelectValue = selectModelValue(
      formValues.fallback_model,
      fallbackModelOptions,
    );
  });

  $effect(() => {
    if (
      sharedSelectedAgentId &&
      sharedSelectedAgentId !== lastSharedSelectedAgentId &&
      agents.some((agent) => agent.id === sharedSelectedAgentId)
    ) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
      if (sharedSelectedAgentId !== selectedAgentId) {
        selectAgent(sharedSelectedAgentId);
      }
    } else if (!sharedSelectedAgentId) {
      lastSharedSelectedAgentId = sharedSelectedAgentId;
    }
  });

  onMount(() => {
    loadCatalogs();
    loadAgents({ preferredAgentId: sharedSelectedAgentId });

    return () => {
      clearAgentAutoSaveTimer();
    };
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

  async function loadCatalogs() {
    try {
      const [modelsResult, connectionsResult, toolsResult, skillsResult] =
        await Promise.all([
          rpc('model.list'),
          rpc('connection.list'),
          rpc('tool.list'),
          rpc('skill.list'),
        ]);

      availableModels = Array.isArray(modelsResult?.models)
        ? modelsResult.models
        : [];
      availableConnections = Array.isArray(connectionsResult?.connections)
        ? connectionsResult.connections
        : [];
      availableTools = Array.isArray(toolsResult?.tools)
        ? toolsResult.tools
        : [];
      availableSkills = Array.isArray(skillsResult?.skills)
        ? skillsResult.skills
        : [];
      invalidSkills = Array.isArray(skillsResult?.invalid_skills)
        ? skillsResult.invalid_skills
        : [];
    } catch (error) {
      errorMessage = viewErrorMessage(error, t('agents.loadError'));
    }
  }

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
    clearAgentAutoSaveTimer();
    selectedAgentId = agentId;
    const agent = agents.find((item) => item.id === agentId) ?? null;

    if (agent) {
      formMode = AGENT_FORM_MODE_EDIT;
      formValues = createAgentFormValues(agent);
      editBaselineValues = createAgentFormValues(agent);
      onAgentSelected?.(agent);
    } else {
      startCreate();
    }

    formErrors = {};
  }

  function startCreate() {
    clearAgentAutoSaveTimer();
    selectedAgentId = '';
    formMode = AGENT_FORM_MODE_CREATE;
    formValues = createAgentFormValues();
    editBaselineValues = createAgentFormValues();
    formErrors = {};
    statusMessage = '';
  }

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
      statusMessage = '';
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
        statusMessage = t('common.alreadySaved', 'Already saved');
      }
      return;
    }

    isSaving = true;
    const draftValues = cloneAgentFormValues(formValues);
    errorMessage = '';

    try {
      const method =
        formMode === AGENT_FORM_MODE_CREATE ? 'agent.create' : 'agent.update';
      const savedAgent = await rpc(method, result.payload);
      statusMessage =
        formMode === AGENT_FORM_MODE_CREATE
          ? t('agents.created', 'Agent created.')
          : t('agents.updated', 'Agent updated.');
      if (formMode === AGENT_FORM_MODE_CREATE) {
        await loadAgents({
          preferredAgentId: savedAgent.id ?? result.payload.id,
        });
      } else {
        applySavedAgentUpdate(savedAgent, result.payload, draftValues);
      }
    } catch (error) {
      errorMessage = viewErrorMessage(error, t('agents.saveError'));
    } finally {
      isSaving = false;
    }
  }

  function shouldAutoSaveAgent() {
    if (
      formMode !== AGENT_FORM_MODE_EDIT ||
      isLoading ||
      isSaving ||
      isDeleting
    ) {
      return false;
    }

    const result = normalizeAgentForm(formValues, {
      mode: AGENT_FORM_MODE_EDIT,
      initialValues: editBaselineValues,
    });

    return result.isValid && agentPayloadHasChanges(result.payload);
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
    const existingAgent =
      agents.find((agent) => agent.id === payload.id) ?? selectedAgent ?? {};
    const nextAgent = {
      ...existingAgent,
      ...payload,
      ...(savedAgent ?? {}),
      id: savedAgent?.id ?? payload.id ?? existingAgent.id,
    };

    agents = agents.map((agent) =>
      agent.id === nextAgent.id ? nextAgent : agent,
    );
    editBaselineValues = createAgentFormValues(nextAgent);

    if (formValuesMatch(formValues, draftValues)) {
      formValues = createAgentFormValues(nextAgent);
    }

    notifyAgentsChanged();
    onAgentSelected?.(nextAgent);
  }

  function formValuesMatch(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
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

  function updateAccessItem(fieldName, itemName, isAllowed) {
    if (fieldName === 'allowed_tools') {
      updateToolAccessItem(itemName, isAllowed);
      return;
    }

    if (fieldName === 'allowed_skills') {
      updateSkillAccessItem(itemName, isAllowed);
    }
  }

  function setAccessItems(fieldName, items, isAllowed) {
    if (fieldName === 'allowed_tools') {
      formValues.allowed_tools = isAllowed ? [WILDCARD_ACCESS] : [];
      return;
    }

    if (fieldName === 'allowed_skills') {
      formValues.allowed_skills = isAllowed ? [WILDCARD_ACCESS] : [];
    }
  }

  function updateToolAccessItem(itemName, isAllowed) {
    const allToolNames = availableTools.map((tool) => tool.name);

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

    if (allToolNames.every((name) => nextItems.includes(name))) {
      formValues.allowed_tools = [WILDCARD_ACCESS];
      return;
    }

    formValues.allowed_tools = nextItems;
  }

  function toolAccessItems() {
    const currentItems = Array.isArray(formValues.allowed_tools)
      ? formValues.allowed_tools
      : [];
    const hasWildcard = currentItems.includes(WILDCARD_ACCESS);
    const allowedItems = hasWildcard ? [] : currentItems;

    return availableTools.map((tool) => ({
      ...tool,
      isAllowed: hasWildcard || allowedItems.includes(tool.name),
    }));
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

    if (allSkillNames.every((name) => nextItems.includes(name))) {
      formValues.allowed_skills = [WILDCARD_ACCESS];
      return;
    }

    formValues.allowed_skills = nextItems;
  }

  function selectModelOptions(selectedModelValue, emptyLabel) {
    const connectionsByProvider = usableConnectionsByProvider();
    const selectedModel = parseModelSelectionValue(selectedModelValue);
    const selectedConnectionId = connectionIdFromModel(
      selectedModel.model,
      selectedModel.connectionLocalId,
    );
    const selectedValue = modelSelectionValue(
      selectedModel.model,
      selectedModel.connectionLocalId,
    );
    const modelExistsInCatalog = availableModels.some(
      (model) => model.id === selectedModel.model,
    );
    const selectedModelOption =
      selectedModel.model &&
      !selectedModel.connectionLocalId &&
      modelExistsInCatalog
        ? {
            value: selectedModel.model,
            label: selectedModel.model,
            isUnavailable: false,
          }
        : null;
    const emptyOption = {
      value: '',
      label: emptyLabel,
      isUnavailable: false,
    };
    const catalogOptions = availableModels.flatMap((model) => {
      const providerConnections =
        connectionsByProvider[model.provider_id] ?? [];

      return providerConnections.map((connection) => ({
        value: modelSelectionValue(
          model.id,
          connectionLocalIdFromConnectionId(connection.id),
        ),
        label: modelOptionLabel(model, connection, providerConnections.length),
        isUnavailable: false,
      }));
    });

    if (
      !selectedValue ||
      catalogOptions.some((option) => option.value === selectedValue) ||
      selectedModelOption
    ) {
      return selectedModelOption
        ? [emptyOption, selectedModelOption, ...catalogOptions]
        : [emptyOption, ...catalogOptions];
    }

    return [
      emptyOption,
      {
        value: selectedValue,
        label: unavailableModelOptionLabel(
          selectedModel.model,
          selectedConnectionId,
        ),
        isUnavailable: true,
      },
      ...catalogOptions,
    ];
  }

  function usableConnectionsByProvider() {
    const connectionsByProvider = {};

    for (const connection of availableConnections) {
      if (!connection?.usable || !connection.provider_id) {
        continue;
      }

      if (!connectionsByProvider[connection.provider_id]) {
        connectionsByProvider[connection.provider_id] = [];
      }

      connectionsByProvider[connection.provider_id].push(connection);
    }

    return connectionsByProvider;
  }

  function modelOptionLabel(model, connection, providerConnectionCount) {
    if (providerConnectionCount <= 1) {
      return model.id;
    }

    return `${model.id} (${connection.label})`;
  }

  function unavailableModelOptionLabel(model, connection) {
    if (!connection) {
      return t(
        'agents.form.modelUnavailableOption',
        'Unavailable / custom: {model}',
        {
          model,
        },
      );
    }

    return t(
      'agents.form.modelUnavailableConnectionOption',
      'Unavailable / custom: {model} ({connection})',
      {
        connection: connectionDisplayLabel(connection),
        model,
      },
    );
  }

  function connectionDisplayLabel(connectionId) {
    const connection = availableConnections.find(
      (item) => item.id === connectionId,
    );

    return connection?.label || connectionId;
  }

  function connectionLocalIdFromConnectionId(connectionId) {
    if (!connectionId) {
      return '';
    }

    const separatorIndex = connectionId.indexOf(':');
    if (separatorIndex === -1) {
      return connectionId;
    }

    return connectionId.slice(separatorIndex + 1);
  }

  function connectionIdFromModel(model, connectionLocalId) {
    if (!model || !connectionLocalId) {
      return '';
    }

    const providerSeparatorIndex = model.indexOf('/');
    if (providerSeparatorIndex === -1) {
      return '';
    }

    const providerId = model.slice(0, providerSeparatorIndex);
    if (!providerId) {
      return '';
    }

    return `${providerId}:${connectionLocalId}`;
  }

  function updateModelSelection(modelFieldName, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    formValues[modelFieldName] = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );
  }

  function selectModelValue(modelValue, options) {
    const selection = parseModelSelectionValue(modelValue);

    if (!selection.model) {
      return '';
    }

    const exactValue = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );

    if (options.some((option) => option.value === exactValue)) {
      return exactValue;
    }

    if (selection.connectionLocalId) {
      return exactValue;
    }

    return selection.model;
  }

  function modelSelectionValue(model, connectionLocalId) {
    if (!model) {
      return '';
    }

    if (!connectionLocalId) {
      return model;
    }

    return `${model}::${connectionLocalId}`;
  }

  function parseModelSelectionValue(selectedValue) {
    if (!selectedValue) {
      return { model: '', connectionLocalId: '' };
    }

    const separatorIndex = selectedValue.lastIndexOf('::');

    if (separatorIndex === -1) {
      return { model: selectedValue, connectionLocalId: '' };
    }

    return {
      model: selectedValue.slice(0, separatorIndex),
      connectionLocalId: selectedValue.slice(separatorIndex + 2),
    };
  }

  function thinkingEffortLabel(option) {
    if (option === '') {
      return t('agents.form.thinkingEffortDefault', EMPTY_VALUE);
    }

    return t(`agents.form.thinkingEffortOption.${option}`, option);
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

  function displayValue(value) {
    return value || EMPTY_VALUE;
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

<section class="agents-view view active" aria-labelledby="agents-list-title">
  <div class="agents-layout">
    <aside class="agent-list-pane" aria-labelledby="agents-list-title">
      <div class="pane-header">
        <span id="agents-list-title" class="pane-title">
          {t('agents.title', 'Agents')}
        </span>
        <button class="btn-new" type="button" onclick={startCreate}>
          <svg viewBox="0 0 14 14" aria-hidden="true">
            <path d="M7 1v12M1 7h12" />
          </svg>
          {t('common.new', 'New')}
        </button>
      </div>

      <div class="agent-list-scroll">
        {#if isLoading}
          <p class="agents-view__list-state">
            {t('agents.loading', 'Loading agents…')}
          </p>
        {:else if agents.length === 0}
          <div class="empty-state agents-view__empty-list">
            <svg
              class="empty-state-icon"
              viewBox="0 0 32 32"
              aria-hidden="true"
            >
              <circle cx="16" cy="10" r="5" />
              <path d="M6 28c0-5.5 4.5-10 10-10s10 4.5 10 10" />
            </svg>
            <div class="empty-state-title">
              {t('agents.empty', 'No agents found.')}
            </div>
            <div class="empty-state-sub">
              {t(
                'agents.emptyCreateHint',
                'Create an agent to begin configuring chat access.',
              )}
            </div>
          </div>
        {:else}
          {#each agents as agent (agent.id)}
            <button
              class:active={agent.id === selectedAgentId}
              class="agent-item"
              type="button"
              onclick={() => selectAgent(agent.id)}
            >
              <div class="agent-bar"></div>
              <div class="agent-item-inner">
                <div class="agent-item-name">{agent.name || agent.id}</div>
                <div class="agent-item-sub">
                  {agent.model || agent.id || t('common.unknown', 'Unknown')}
                </div>
              </div>
            </button>
          {/each}
        {/if}
      </div>
    </aside>

    <form class="agent-detail-pane" onsubmit={saveAgent}>
      <div class="agent-detail-scroll">
        <div class="detail-top">
          <div>
            <div class="detail-heading">
              {formMode === AGENT_FORM_MODE_CREATE
                ? t('agents.create', 'Create Agent')
                : selectedAgent?.name || formValues.name || selectedAgent?.id}
            </div>
            <div class="detail-sub">{detailSubtitle}</div>
          </div>

          <div class="detail-btns">
            {#if formMode === AGENT_FORM_MODE_EDIT}
              <button
                class="btn-outline btn-dang"
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
        </div>

        {#if errorMessage}
          <p
            class="agents-view__notice agents-view__notice--error"
            role="alert"
          >
            {errorMessage}
          </p>
        {/if}

        {#if statusMessage}
          <p class="agents-view__notice" role="status">{statusMessage}</p>
        {/if}

        <div class="detail-group">
          <div class="detail-group-title">
            {t('agents.detail.identity', 'Identity')}
          </div>
          <div class="detail-fields">
            <label class="f">
              <span class="f-label">{t('agents.form.id', 'Agent ID')}</span>
              <input
                class:agents-view__invalid={formErrors.id}
                class="s-input"
                type="text"
                bind:value={formValues.id}
                disabled={formMode === AGENT_FORM_MODE_EDIT}
                aria-describedby="agent-id-help agent-id-error"
              />
              <small id="agent-id-help" class="agents-view__field-help">
                {t(
                  'agents.form.idHelp',
                  'Agent IDs are immutable after creation.',
                )}
              </small>
              {#if formErrors.id}
                <small id="agent-id-error" class="agents-view__field-error">
                  {fieldError('id')}
                </small>
              {/if}
            </label>

            <label class="f">
              <span class="f-label">{t('agents.form.name', 'Name')}</span>
              <input
                class:agents-view__invalid={formErrors.name}
                class="s-input"
                type="text"
                bind:value={formValues.name}
              />
              {#if formErrors.name}
                <small class="agents-view__field-error"
                  >{fieldError('name')}</small
                >
              {/if}
            </label>

            <div class="f wide">
              <div class="f-label">
                {t('agents.form.workspace', 'Workspace')}
              </div>
              {#if formValues.workspace}
                <div class="f-value mono agents-view__wrap-value">
                  {formValues.workspace}
                </div>
              {:else}
                <div class="f-value agents-view__muted-value">
                  {t(
                    'agents.form.workspaceAssignedByServer',
                    'Workspace is assigned by the server when the agent is created.',
                  )}
                </div>
              {/if}
              <small class="agents-view__field-help">
                {t(
                  'agents.form.workspaceReadOnly',
                  'Workspace is read-only in this WebUI.',
                )}
              </small>
            </div>
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
                emptyLabel={t(
                  'agents.form.modelSearchEmpty',
                  'No models match',
                )}
                ariaLabel={t('agents.form.model', 'Model')}
                triggerClass="agents-view__dropdown"
                panelClass="agents-view__search-panel"
                onValueChange={(selectedValue) =>
                  updateModelSelection('model', selectedValue)}
              />
            </label>

            <label class="f">
              <span class="f-label"
                >{t('agents.form.fallbackModel', 'Fallback model')}</span
              >
              <SearchableDropdown
                id="agent-fallback-model"
                value={fallbackModelSelectValue}
                options={fallbackModelOptions}
                placeholder={t('agents.form.fallbackModelPlaceholder', 'None')}
                searchPlaceholder={t(
                  'agents.form.modelSearchPlaceholder',
                  'Filter models…',
                )}
                emptyLabel={t(
                  'agents.form.modelSearchEmpty',
                  'No models match',
                )}
                ariaLabel={t('agents.form.fallbackModel', 'Fallback model')}
                triggerClass="agents-view__dropdown"
                panelClass="agents-view__search-panel"
                onValueChange={(selectedValue) =>
                  updateModelSelection('fallback_model', selectedValue)}
              />
            </label>

            <label class="f agents-view__thinking-field">
              <span class="f-label"
                >{t('agents.form.thinkingEffort', 'Thinking effort')}</span
              >
              <Dropdown
                id="agent-thinking-effort"
                value={formValues.thinking_effort}
                options={thinkingEffortOptions}
                ariaLabel={t('agents.form.thinkingEffort', 'Thinking effort')}
                triggerClass="agents-view__dropdown"
                listClass="agents-view__thinking-list"
                onValueChange={(selectedValue) => {
                  formValues.thinking_effort = selectedValue;
                }}
              />
            </label>

            <label class="f">
              <span class="f-label"
                >{t('agents.form.temperature', 'Temperature')}</span
              >
              <input
                class:agents-view__invalid={formErrors.temperature}
                class="s-input"
                type="number"
                step="0.01"
                bind:value={formValues.temperature}
              />
              {#if formErrors.temperature}
                <small class="agents-view__field-error">
                  {fieldError('temperature')}
                </small>
              {/if}
            </label>

            <div class="f">
              <div class="f-label">
                {t('agents.detail.fallbackStatus', 'Fallback')}
              </div>
              <div
                class:f-value={formValues.fallback_model}
                class:agents-view__muted-value={!formValues.fallback_model}
              >
                {displayValue(formValues.fallback_model)}
              </div>
            </div>

            <div class="f">
              <div class="f-label">
                {t('agents.detail.thinkingStatus', 'Thinking')}
              </div>
              {#if formValues.thinking_effort}
                <div class="f-value">
                  <span class="chip chip-orange"
                    >{formValues.thinking_effort}</span
                  >
                </div>
              {:else}
                <div class="agents-view__muted-value">{EMPTY_VALUE}</div>
              {/if}
            </div>
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
                <button
                  class="tl-btn"
                  type="button"
                  disabled={visibleToolItems.length === 0}
                  onclick={() =>
                    setAccessItems('allowed_tools', visibleToolItems, true)}
                >
                  {t('agents.access.allOn', 'all on')}
                </button>
                <button
                  class="tl-btn"
                  type="button"
                  disabled={visibleToolItems.length === 0}
                  onclick={() =>
                    setAccessItems('allowed_tools', visibleToolItems, false)}
                >
                  {t('agents.access.allOff', 'all off')}
                </button>
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
                          {t(
                            'agents.access.descriptionLabel',
                            '{description}',
                            { description: item.description },
                          )}
                        </span>
                      {/if}
                    </div>
                    <button
                      class="tl-toggle"
                      class:on={item.isAllowed}
                      type="button"
                      role="switch"
                      aria-checked={item.isAllowed}
                      aria-label={t(
                        'agents.access.toggleTool',
                        'Toggle tool {name}',
                        {
                          name: item.name,
                        },
                      )}
                      disabled={item.isWildcard}
                      onclick={() =>
                        updateAccessItem(
                          'allowed_tools',
                          item.name,
                          !item.isAllowed,
                        )}
                    >
                      <span class="t-knob"></span>
                    </button>
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
                <button
                  class="tl-btn"
                  type="button"
                  disabled={visibleSkillItems.length === 0}
                  onclick={() =>
                    setAccessItems('allowed_skills', visibleSkillItems, true)}
                >
                  {t('agents.access.allOn', 'all on')}
                </button>
                <button
                  class="tl-btn"
                  type="button"
                  disabled={visibleSkillItems.length === 0}
                  onclick={() =>
                    setAccessItems('allowed_skills', visibleSkillItems, false)}
                >
                  {t('agents.access.allOff', 'all off')}
                </button>
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
                          {t(
                            'agents.access.descriptionLabel',
                            '{description}',
                            { description: item.description },
                          )}
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
                    <button
                      class="tl-toggle"
                      class:on={item.isAllowed}
                      type="button"
                      role="switch"
                      aria-checked={item.isAllowed}
                      aria-label={t(
                        'agents.access.toggleSkill',
                        'Toggle skill {name}',
                        {
                          name: item.name,
                        },
                      )}
                      disabled={item.isWildcard}
                      onclick={() =>
                        updateAccessItem(
                          'allowed_skills',
                          item.name,
                          !item.isAllowed,
                        )}
                    >
                      <span class="t-knob"></span>
                    </button>
                  </div>
                {/each}
              </div>
            {:else}
              <p class="agents-view__placeholder-row">
                {t(
                  'agents.access.noSkills',
                  'No loadable skills are available.',
                )}
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
                            t(
                              'agents.access.unknownSkillName',
                              'Unknown skill',
                            )}
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
                      <span class="chip chip-amber">
                        {t('agents.access.notLoadable', 'not loadable')}
                      </span>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}
          </div>

          <div class="detail-fields agents-view__access-meta">
            <div class="f wide">
              <div class="f-label">
                {t('agents.form.workspace', 'Workspace')}
              </div>
              <div class="f-value mono agents-view__wrap-value">
                {displayValue(formValues.workspace)}
              </div>
            </div>
          </div>
        </div>

        <div class="detail-group">
          <div class="detail-group-title">
            {t('agents.detail.session', 'Session')}
          </div>
          <div class="detail-fields">
            <div class="f wide">
              <div class="f-label">
                {t('agents.detail.sessionId', 'Session ID')}
              </div>
              <div class="f-value mono agents-view__wrap-value">
                {displayValue(selectedAgent?.current_session_id)}
              </div>
            </div>
            <div class="f">
              <div class="f-label">{t('agents.detail.created', 'Created')}</div>
              <div class="f-value mono agents-view__wrap-value">
                {displayValue(selectedAgent?.created_at)}
              </div>
            </div>
            <div class="f">
              <div class="f-label">{t('agents.detail.updated', 'Updated')}</div>
              <div class="f-value mono agents-view__wrap-value">
                {displayValue(selectedAgent?.updated_at)}
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

        <div class="agent-sticky-footer">
          <button class="btn-outline" type="submit" disabled={isSaving}>
            {isSaving ? t('common.saving', 'Saving…') : submitLabel}
          </button>
        </div>
      </div>
    </form>
  </div>
</section>

<style>
  .agents-view {
    display: flex;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    color: var(--text-hi);
    background: var(--bg);
  }

  .agents-layout {
    display: flex;
    min-height: 0;
    min-width: 0;
    flex: 1;
    height: 100%;
    overflow: hidden;
  }

  .agent-list-pane {
    display: flex;
    min-height: 0;
    width: 240px;
    min-width: 240px;
    flex-direction: column;
    overflow: hidden;
    border-right: 1px solid var(--border);
    background: var(--surface);
  }

  .pane-header {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px 10px;
    border-bottom: 1px solid var(--border);
  }

  .btn-new svg {
    width: 11px;
    height: 11px;
  }

  .agent-list-scroll {
    min-height: 0;
    flex: 1;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 4px 0;
    scrollbar-gutter: stable;
  }

  .agent-item {
    display: flex;
    width: 100%;
    align-items: stretch;
    border: 0;
    color: inherit;
    background: transparent;
    text-align: left;
    transition: background 100ms ease;
  }

  .agent-item:hover,
  .agent-item:focus-visible {
    background: var(--surface-2);
  }

  .agent-item:focus-visible {
    outline: 1px solid rgba(232, 135, 10, 0.4);
    outline-offset: -1px;
  }

  .agent-item.active {
    background: var(--accent-dim);
  }

  .agent-bar {
    width: 2px;
    flex-shrink: 0;
    background: transparent;
  }

  .agent-item.active .agent-bar {
    background: var(--accent);
  }

  .agent-item-inner {
    min-width: 0;
    flex: 1;
    padding: 7px 12px 7px 10px;
  }

  .agent-item-name {
    overflow: hidden;
    color: var(--text-hi);
    font-size: 13px;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-item.active .agent-item-name {
    color: var(--accent);
  }

  .agent-item-sub {
    overflow: hidden;
    margin-top: 1px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-detail-pane {
    display: flex;
    min-height: 0;
    min-width: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
  }

  .agent-detail-scroll {
    display: flex;
    min-height: 0;
    min-width: 0;
    flex: 1;
    flex-direction: column;
    gap: 22px;
    overflow-y: auto;
    overscroll-behavior: contain;
    padding: 26px 30px;
    scrollbar-gutter: stable;
  }

  .detail-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--space-lg);
  }

  .detail-heading {
    color: var(--text-hi);
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.03em;
    line-height: 1.2;
  }

  .detail-sub {
    margin-top: 4px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .detail-btns {
    display: flex;
    flex-shrink: 0;
    gap: 8px;
  }

  .agent-sticky-footer {
    position: sticky;
    bottom: 0;
    display: flex;
    justify-content: flex-end;
    padding: 16px 0 4px;
    background: var(--surface);
  }

  .detail-group {
    flex-shrink: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
  }

  .agents-view__model-group,
  .agents-view__model-fields {
    position: relative;
  }

  .agents-view__model-group {
    overflow: visible;
    z-index: 1;
  }

  .detail-group-title {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .detail-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    padding: 16px;
  }

  .f {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 4px;
  }

  .f.wide {
    grid-column: 1 / -1;
  }

  .f-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    letter-spacing: 0.05em;
    line-height: 1;
    text-transform: uppercase;
  }

  .f-value {
    color: var(--text-hi);
    font-size: 13.5px;
    font-weight: 500;
  }

  .f-value.mono {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .tl-section {
    display: flex;
    flex-direction: column;
  }

  .tl-section + .tl-section {
    border-top: 1px solid var(--border);
  }

  .tl-section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px 8px;
  }

  .tl-actions {
    display: flex;
    gap: 6px;
  }

  .tl-btn {
    padding: 2px 8px;
    font-family: var(--font-mono);
    font-size: 10px;
  }

  .tl-items {
    display: flex;
    flex-direction: column;
    border-top: 1px solid var(--border);
  }

  .tl-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 16px;
    border-bottom: 1px solid var(--border);
  }

  .tl-item:last-child {
    border-bottom: 0;
  }

  .tl-item-name {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .agents-view__access-copy {
    display: flex;
    min-width: 0;
    flex: 1;
    flex-direction: column;
    gap: 3px;
  }

  .agents-view__access-description {
    color: var(--text-lo);
    font-size: 12px;
    line-height: 1.4;
  }

  .agents-view__skill-warnings {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 3px;
    padding: 7px 9px;
    border: 1px solid rgba(245, 158, 11, 0.18);
    border-left: 2px solid var(--amber);
    border-radius: var(--r-sm);
    color: var(--amber);
    background: rgba(245, 158, 11, 0.06);
    font-size: 11.5px;
    line-height: 1.35;
  }

  .agents-view__skill-warnings ul {
    margin: 0;
    padding-left: 16px;
  }

  .agents-view__warning-label,
  .agents-view__invalid-skills-title {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.07em;
    line-height: 1;
    text-transform: uppercase;
  }

  .agents-view__invalid-skills {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 16px 14px;
    border-top: 1px solid var(--border);
  }

  .agents-view__invalid-skills-title {
    color: var(--text-lo);
  }

  .agents-view__invalid-skills-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .agents-view__invalid-skill {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 10px;
    border: 1px solid rgba(245, 158, 11, 0.18);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .agents-view__invalid-skill-path {
    overflow-wrap: anywhere;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
    line-height: 1.35;
  }

  .agents-view__access-meta {
    border-top: 1px solid var(--border);
  }

  :global(.agents-view__dropdown) {
    width: 100%;
  }

  :global(.agents-view__dropdown.open) {
    z-index: 12;
  }

  :global(.agents-view__search-panel) {
    z-index: 220;
  }

  .agents-view__thinking-field {
    position: relative;
    z-index: 2;
  }

  :global(.agents-view__thinking-list) {
    max-height: 280px;
    overflow-y: auto;
  }

  .agents-view__notice,
  .agents-view__placeholder-row,
  .agents-view__list-state {
    margin: 0;
    color: var(--text-med);
    font-size: 12.5px;
    line-height: 1.4;
  }

  .agents-view__notice {
    padding: 11px 14px;
    border: 1px solid var(--border-2);
    border-left: 2px solid var(--green);
    border-radius: var(--r-md);
    background: var(--surface);
  }

  .agents-view__notice--error {
    border-left-color: var(--red);
    color: var(--red);
  }

  .agents-view__placeholder-row,
  .agents-view__list-state {
    padding: 12px 16px;
    color: var(--text-lo);
  }

  .agents-view__empty-list {
    min-height: 220px;
  }

  .empty-state-icon {
    width: 34px;
    height: 34px;
  }

  .agents-view__field-help {
    color: var(--text-lo);
    font-size: 12px;
    line-height: 1.4;
  }

  .agents-view__field-error {
    color: var(--red);
    font-size: 12px;
    line-height: 1.4;
  }

  .agents-view__invalid {
    border-color: var(--red) !important;
  }

  .agents-view__muted-value {
    color: var(--text-lo);
    font-size: 13px;
  }

  .agents-view__wrap-value {
    overflow-wrap: anywhere;
  }

  @media (max-width: 860px) {
    .agents-layout,
    .detail-top {
      flex-direction: column;
    }

    .agent-list-pane {
      width: 100%;
      min-width: 0;
      max-height: min(240px, 35vh);
      border-right: 0;
      border-bottom: 1px solid var(--border);
    }

    .agent-detail-scroll {
      padding: 22px 18px;
    }

    .detail-fields {
      grid-template-columns: 1fr;
    }

    .detail-btns {
      flex-wrap: wrap;
    }
  }
</style>
