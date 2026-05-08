<script>
  import { onMount } from 'svelte';

  import { rpc } from '$lib/api.js';
  import {
    AGENT_FORM_MODE_CREATE,
    AGENT_FORM_MODE_EDIT,
    createAgentFormValues,
    normalizeAgentForm,
    textToList,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';

  const EMPTY_VALUE = '—';
  const MODEL_CONNECTION_VALUE_SEPARATOR = '\u001f';
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
  let formMode = $state(AGENT_FORM_MODE_CREATE);
  let formValues = $state(createAgentFormValues());
  let formErrors = $state({});
  let isLoading = $state(false);
  let isSaving = $state(false);
  let isDeleting = $state(false);
  let errorMessage = $state('');
  let statusMessage = $state('');
  let availableModels = $state([]);
  let availableConnections = $state([]);
  let availableTools = $state([]);
  let modelSelectValue = $state('');
  let fallbackModelSelectValue = $state('');

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
  let visibleSkillItems = $derived(accessItems(formValues.allowed_skills));
  let modelOptions = $derived(
    selectModelOptions(formValues.model, formValues.connection),
  );
  let fallbackModelOptions = $derived(
    selectModelOptions(
      formValues.fallback_model,
      formValues.fallback_connection,
    ),
  );

  $effect(() => {
    modelSelectValue = selectModelValue(
      formValues.model,
      formValues.connection,
      modelOptions,
    );
    fallbackModelSelectValue = selectModelValue(
      formValues.fallback_model,
      formValues.fallback_connection,
      fallbackModelOptions,
    );
  });

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
    loadCatalogs();
    loadAgents({ preferredAgentId: sharedSelectedAgentId });
  });

  async function loadCatalogs() {
    try {
      const [modelsResult, connectionsResult, toolsResult] = await Promise.all([
        rpc('model.list'),
        rpc('connection.list'),
        rpc('tool.list'),
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
    selectedAgentId = agentId;
    const agent = agents.find((item) => item.id === agentId) ?? null;

    if (agent) {
      formMode = AGENT_FORM_MODE_EDIT;
      formValues = createConnectionAgentFormValues(agent);
      onAgentSelected?.(agent);
    } else {
      startCreate();
    }

    formErrors = {};
  }

  function startCreate() {
    selectedAgentId = '';
    formMode = AGENT_FORM_MODE_CREATE;
    formValues = createConnectionAgentFormValues();
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

    result.payload.connection = asText(formValues.connection).trim();
    result.payload.fallback_connection = asText(
      formValues.fallback_connection,
    ).trim();

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

  function updateAccessItem(fieldName, itemName, isAllowed) {
    if (fieldName === 'allowed_tools') {
      updateToolAccessItem(itemName, isAllowed);
      return;
    }

    const currentItems = textToList(formValues[fieldName]);
    const nextItems = currentItems.filter((item) => item !== WILDCARD_ACCESS);
    const existingIndex = nextItems.indexOf(itemName);

    if (isAllowed && existingIndex === -1) {
      nextItems.push(itemName);
    }

    if (!isAllowed && existingIndex !== -1) {
      nextItems.splice(existingIndex, 1);
    }

    formValues[fieldName] = nextItems.join('\n');
  }

  function setAccessItems(fieldName, items, isAllowed) {
    if (fieldName === 'allowed_tools') {
      formValues.allowed_tools = isAllowed ? [WILDCARD_ACCESS] : [];
      return;
    }

    formValues[fieldName] = isAllowed
      ? items.map((item) => item.name).join('\n')
      : '';
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

  function accessItems(text) {
    const items = textToList(text);

    if (items.length === 0) {
      return [];
    }

    if (items.includes(WILDCARD_ACCESS)) {
      return [{ name: WILDCARD_ACCESS, isAllowed: true, isWildcard: true }];
    }

    return items.map((item) => ({
      name: item,
      isAllowed: true,
      isWildcard: false,
    }));
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

  function createConnectionAgentFormValues(agent = {}) {
    const values = createAgentFormValues(agent);
    values.connection = asText(agent.connection);
    values.fallback_connection = asText(agent.fallback_connection);

    return values;
  }

  function selectModelOptions(selectedModel, selectedConnection) {
    const connectionsByProvider = usableConnectionsByProvider();
    const selectedValue = modelSelectionValue(
      selectedModel,
      selectedConnection,
    );
    const catalogOptions = availableModels.flatMap((model) => {
      const providerConnections =
        connectionsByProvider[model.provider_id] ?? [];

      return providerConnections.map((connection) => ({
        value: modelSelectionValue(model.id, connection.id),
        label: modelOptionLabel(model, connection, providerConnections.length),
        isUnavailable: false,
      }));
    });

    if (
      !selectedValue ||
      catalogOptions.some((option) => option.value === selectedValue)
    ) {
      return catalogOptions;
    }

    return [
      {
        value: selectedValue,
        label: unavailableModelOptionLabel(selectedModel, selectedConnection),
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

  function updateModelSelection(
    modelFieldName,
    connectionFieldName,
    selectedValue,
  ) {
    const selection = parseModelSelectionValue(selectedValue);
    formValues[modelFieldName] = selection.model;
    formValues[connectionFieldName] = selection.connection;
  }

  function selectModelValue(model, connection, options) {
    if (!model) {
      return '';
    }

    const exactValue = modelSelectionValue(model, connection);

    if (options.some((option) => option.value === exactValue)) {
      return exactValue;
    }

    if (connection) {
      return exactValue;
    }

    return model;
  }

  function modelSelectionValue(model, connection) {
    if (!model) {
      return '';
    }

    return `${model}${MODEL_CONNECTION_VALUE_SEPARATOR}${connection || ''}`;
  }

  function parseModelSelectionValue(selectedValue) {
    if (!selectedValue) {
      return { model: '', connection: '' };
    }

    const separatorIndex = selectedValue.indexOf(
      MODEL_CONNECTION_VALUE_SEPARATOR,
    );

    if (separatorIndex === -1) {
      return { model: selectedValue, connection: '' };
    }

    return {
      model: selectedValue.slice(0, separatorIndex),
      connection: selectedValue.slice(
        separatorIndex + MODEL_CONNECTION_VALUE_SEPARATOR.length,
      ),
    };
  }

  function thinkingEffortLabel(option) {
    if (option === '') {
      return t('agents.form.thinkingEffortDefault', 'Default');
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

  function asText(value) {
    return value === null || value === undefined ? '' : String(value);
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
            <button class="btn-outline" type="submit" disabled={isSaving}>
              {isSaving ? t('common.saving', 'Saving…') : submitLabel}
            </button>
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

        <div class="detail-group">
          <div class="detail-group-title">
            {t('agents.detail.model', 'Model')}
          </div>
          <div class="detail-fields">
            <label class="f wide">
              <span class="f-label">{t('agents.form.model', 'Model')}</span>
              <div class="agents-view__select-wrap">
                <select
                  class="s-input agents-view__select"
                  bind:value={modelSelectValue}
                  onchange={() =>
                    updateModelSelection(
                      'model',
                      'connection',
                      modelSelectValue,
                    )}
                >
                  <option value="">
                    {t(
                      'agents.form.modelPlaceholder',
                      'Default (no model selected)',
                    )}
                  </option>
                  {#each modelOptions as option (option.value)}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
                <span class="agents-view__select-icon" aria-hidden="true">
                  <svg
                    class="dropdown-chevron"
                    width="12"
                    height="12"
                    viewBox="0 0 12 12"
                  >
                    <path d="M2 4l4 4 4-4" />
                  </svg>
                </span>
              </div>
            </label>

            <label class="f">
              <span class="f-label"
                >{t('agents.form.fallbackModel', 'Fallback model')}</span
              >
              <div class="agents-view__select-wrap">
                <select
                  class="s-input agents-view__select"
                  bind:value={fallbackModelSelectValue}
                  onchange={() =>
                    updateModelSelection(
                      'fallback_model',
                      'fallback_connection',
                      fallbackModelSelectValue,
                    )}
                >
                  <option value="">
                    {t('agents.form.fallbackModelPlaceholder', 'None')}
                  </option>
                  {#each fallbackModelOptions as option (option.value)}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
                <span class="agents-view__select-icon" aria-hidden="true">
                  <svg
                    class="dropdown-chevron"
                    width="12"
                    height="12"
                    viewBox="0 0 12 12"
                  >
                    <path d="M2 4l4 4 4-4" />
                  </svg>
                </span>
              </div>
            </label>

            <label class="f">
              <span class="f-label"
                >{t('agents.form.thinkingEffort', 'Thinking effort')}</span
              >
              <div class="agents-view__select-wrap">
                <select
                  class="s-input agents-view__select"
                  bind:value={formValues.thinking_effort}
                >
                  {#each THINKING_EFFORT_OPTIONS as option (option)}
                    <option value={option}>{thinkingEffortLabel(option)}</option
                    >
                  {/each}
                </select>
                <span class="agents-view__select-icon" aria-hidden="true">
                  <svg
                    class="dropdown-chevron"
                    width="12"
                    height="12"
                    viewBox="0 0 12 12"
                  >
                    <path d="M2 4l4 4 4-4" />
                  </svg>
                </span>
              </div>
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
                    <span class="tl-item-name">{item.name}</span>
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
                  'No backend skill catalog is available; add skill names below.',
                )}
              </p>
            {/if}
            <label class="agents-view__access-editor">
              <span>{t('agents.form.allowedSkills', 'Allowed skills')}</span>
              <textarea rows="4" bind:value={formValues.allowed_skills}
              ></textarea>
              <small
                >{t('agents.form.listHelp', 'Enter one item per line.')}</small
              >
            </label>
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

  .detail-group {
    flex-shrink: 0;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
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

  .f-label,
  .agents-view__access-editor span {
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

  .agents-view__access-editor {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 12px 16px 16px;
    border-top: 1px solid var(--border);
  }

  .agents-view__access-editor textarea {
    min-height: 82px;
    resize: vertical;
  }

  .agents-view__access-meta {
    border-top: 1px solid var(--border);
  }

  .agents-view__select-wrap {
    position: relative;
    width: 100%;
  }

  .agents-view__select {
    width: 100%;
    padding-right: 34px;
    appearance: none;
    cursor: pointer;
  }

  .agents-view__select-icon {
    position: absolute;
    top: 50%;
    right: 11px;
    display: inline-flex;
    width: 16px;
    height: 16px;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    color: var(--text-lo);
    pointer-events: none;
    transform: translateY(-50%);
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

  .agents-view__field-help,
  .agents-view__access-editor small {
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
