<script>
  import { t } from '$lib/i18n.js';
  import {
    AGENT_FORM_MODE_EDIT,
    reasoningForModelValue,
    effortOptionsForReasoning,
  } from '$lib/agentForm.js';
  import Button from '../ui/Button.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Dropdown from '../Dropdown.svelte';
  import TextField from '../ui/TextField.svelte';
  import {
    selectModelValue,
    filterModelSelectOptions,
    buildModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
  } from '$lib/modelSelection.js';
  let {
    availableModels,
    availableConnections,
    projectOptions,
    projectCatalogError,
    onModelDropdownOpenChange,
    formValues = $bindable(),
    formMode,
    formErrors,
    fieldError,
    navigateToAgentDefaults,
    inheritSource,
    inheritDisplayValue,
  } = $props();

  const EMPTY_VALUE = '—';

  let showAllModels = $state(false);

  let allModelOptions = $derived(
    selectModelOptions(formValues.model, inheritModelLabel('model')),
  );

  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, {
      showAll: showAllModels,
      selectedModelValue: formValues.model,
    }),
  );

  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
      translate: t,
    }),
  );

  let modelSelectValue = $derived(
    selectModelValue(formValues.model, modelOptions),
  );

  // The control column is narrow; the full choice (an inherited default with
  // its source, or a long Model id) stays readable on hover.
  let modelTriggerTooltip = $derived(
    modelOptions.find((option) => option.value === modelSelectValue)?.label ||
      inheritModelLabel('model'),
  );

  // The fallback chain binds to the raw ordered list. Every row shares one
  // unfiltered option catalog — a chain is short (max 5) and rows stay
  // comparable, so no per-row show/hide footer is needed.
  const MAX_FALLBACK_MODEL_ROWS = 5;

  let allFallbackModelOptions = $derived(
    selectModelOptions(
      '',
      t('agents.form.fallbackModelInherit', 'Inherit global default'),
    ),
  );

  let fallbackModelRows = $derived(
    formValues.fallback_models.map((binding) => ({
      binding,
      selectValue: selectModelValue(binding, allFallbackModelOptions),
    })),
  );

  let canAddFallbackModelRow = $derived(
    formValues.fallback_models.length < MAX_FALLBACK_MODEL_ROWS,
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

  let temperatureIsInherit = $derived(formValues.temperature === '');

  let temperatureDescribedBy = $derived(
    [
      'agent-temperature-desc',
      temperatureIsInherit ? 'agent-temperature-help' : '',
      formErrors.temperature ? 'agent-temperature-error' : '',
    ]
      .filter(Boolean)
      .join(' '),
  );

  let projectDropdownOptions = $derived(buildProjectDropdownOptions());

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

  function updateFallbackModelEntry(index, selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    const next = [...formValues.fallback_models];
    next[index] = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );
    formValues.fallback_models = next;
  }

  function addFallbackModelEntry() {
    formValues.fallback_models = [...formValues.fallback_models, ''];
  }

  function removeFallbackModelEntry(index) {
    formValues.fallback_models = formValues.fallback_models.filter(
      (_, entryIndex) => entryIndex !== index,
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

  let modelOptionsOpen = $state(false);
  $effect(() => {
    if (formErrors.temperature) modelOptionsOpen = true;
  });

  function clearTemperature() {
    formValues.temperature = '';
  }
</script>

<div class="agents-view__part" id="agent-detail-panel-overview">
  <section class="s-section" aria-labelledby="agent-section-identity">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-identity">
        {t('agents.detail.identity', 'Identity')}
      </h3>
    </header>
    <div class="s-section__body">
      <div class="s-group">
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-name">
              {t('agents.form.name', 'Name')}
            </label>
            {#if formErrors.name}
              <p
                class="agents-view__row-error"
                id="agent-name-error"
                role="alert"
              >
                {fieldError('name')}
              </p>
            {/if}
          </div>
          <div class="s-row-control">
            <TextField
              id="agent-name"
              invalid={Boolean(formErrors.name)}
              aria-describedby={formErrors.name
                ? 'agent-name-error'
                : undefined}
              value={formValues.name}
              onInput={(next) => (formValues.name = next)}
            />
          </div>
        </div>

        {#if formMode === AGENT_FORM_MODE_EDIT}
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="agent-project">
                {t('agents.form.project', 'Project')}
              </label>
              <div class="s-row-desc" id="agent-project-help">
                {projectCatalogError
                  ? t(
                      'agents.form.projectUnavailableHelp',
                      'The saved selection is preserved. Project editing is unavailable until the catalog reloads.',
                    )
                  : t(
                      'agents.form.projectHelp',
                      'Where relative file and shell work runs. Workspace remains the identity and memory home.',
                    )}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="agent-project"
                value={formValues.root_project_id ?? ''}
                options={projectDropdownOptions}
                disabled={Boolean(projectCatalogError)}
                ariaLabel={t('agents.form.project', 'Project')}
                ariaDescribedby="agent-project-help"
                triggerClass="agents-view__dropdown"
                onValueChange={(selectedValue) => {
                  formValues.root_project_id = selectedValue || null;
                }}
              />
            </div>
          </div>
        {/if}
      </div>
    </div>
  </section>

  <section class="s-section" aria-labelledby="agent-section-model">
    <header class="s-section__head">
      <h3 class="s-section__title" id="agent-section-model">
        {t('agents.detail.model', 'Model')}
      </h3>
      {#if formMode === AGENT_FORM_MODE_EDIT}
        <div class="s-section__aside">
          <Button variant="tertiary" onClick={navigateToAgentDefaults}>
            {t('inherit.editGlobalDefaults', 'Edit global defaults')}
          </Button>
        </div>
      {/if}
    </header>
    <div class="s-section__body">
      <div class="s-group agents-view__model-group">
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-model">
              {t('agents.form.model', 'Model')}
            </label>
          </div>
          <div class="s-row-control">
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
              triggerTooltip={modelTriggerTooltip}
              panelClass="agents-view__search-panel"
              footerActionLabel={modelFilterFooter}
              onFooterAction={() => (showAllModels = !showAllModels)}
              onOpenChange={onModelDropdownOpenChange}
              onValueChange={(selectedValue) =>
                updateModelSelection('model', selectedValue)}
            />
          </div>
        </div>

        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-thinking-effort">
              {t('agents.form.thinkingEffort', 'Thinking effort')}
            </label>
            <div class="s-row-desc" id="agent-thinking-effort-help">
              {effortDropdownDisabled
                ? t(
                    'agents.form.thinkingEffortUnsupported',
                    'This model does not support reasoning.',
                  )
                : t(
                    'agents.form.thinkingEffortDescription',
                    'How much internal reasoning the Model may spend before answering.',
                  )}
            </div>
          </div>
          <div class="s-row-control">
            <Dropdown
              id="agent-thinking-effort"
              value={formValues.thinking_effort}
              options={thinkingEffortOptions}
              disabled={effortDropdownDisabled}
              ariaLabel={t('agents.form.thinkingEffort', 'Thinking effort')}
              ariaDescribedby="agent-thinking-effort-help"
              triggerClass="agents-view__dropdown"
              listClass="agents-view__thinking-list"
              onValueChange={(selectedValue) => {
                formValues.thinking_effort = selectedValue;
              }}
            />
          </div>
        </div>

        <button
          type="button"
          class="s-row s-row--compact s-disclosure-row"
          id="agent-model-options-toggle"
          aria-expanded={modelOptionsOpen}
          aria-controls="agent-model-options"
          onclick={() => (modelOptionsOpen = !modelOptionsOpen)}
        >
          <span class="s-row-label">
            <span
              class="disclosure-chevron"
              class:disclosure-chevron--open={modelOptionsOpen}
              aria-hidden="true"
            ></span>
            {t('agents.modelOptions', 'Temperature & fallback models')}
          </span>
        </button>
        <div
          class="s-group__rows"
          id="agent-model-options"
          hidden={!modelOptionsOpen}
        >
          <div class="s-row">
            <div class="s-row-info">
              <label class="s-row-label" for="agent-temperature">
                {t('agents.form.temperature', 'Temperature')}
              </label>
              <div class="s-row-desc" id="agent-temperature-desc">
                {t(
                  'agents.form.temperatureDescription',
                  'Sampling randomness, typically 0–2.',
                )}
              </div>
              {#if temperatureIsInherit}
                <div
                  class="s-row-desc agents-view__inherit-hint"
                  id="agent-temperature-help"
                >
                  {inheritSource('temperature') === 'global_default'
                    ? t('inherit.hint', 'Inherited: {value} (global default)', {
                        value: inheritDisplayValue('temperature'),
                      })
                    : t(
                        'inherit.hintProviderDefault',
                        'Provider default — nothing is set here or in the global defaults.',
                      )}
                </div>
              {/if}
              {#if formErrors.temperature}
                <p
                  class="agents-view__row-error"
                  id="agent-temperature-error"
                  role="alert"
                >
                  {fieldError('temperature')}
                </p>
              {/if}
            </div>
            <div class="s-row-control agents-view__temperature-control">
              <TextField
                id="agent-temperature"
                inputmode="decimal"
                invalid={Boolean(formErrors.temperature)}
                aria-describedby={temperatureDescribedBy}
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
          </div>

          <div class="s-row agents-view__fallback-models">
            <div class="s-row-info">
              <div class="s-row-label" id="agent-fallback-models-label">
                {t('agents.form.fallbackModels', 'Fallback models')}
              </div>
              <div class="s-row-desc">
                {t(
                  'agents.form.fallbackModelsHelp',
                  'Tried in order when the primary model fails or is unavailable. The first entry has the highest priority.',
                )}
              </div>
            </div>
            <div class="s-row-control agents-view__fallback-list">
              {#each fallbackModelRows as row, index (index)}
                <div class="agents-view__fallback-row">
                  <SearchableDropdown
                    id={`agent-fallback-model-${index}`}
                    value={row.selectValue}
                    options={allFallbackModelOptions}
                    placeholder={t(
                      'agents.form.fallbackModelPlaceholder',
                      'None',
                    )}
                    searchPlaceholder={t(
                      'agents.form.modelSearchPlaceholder',
                      'Filter models…',
                    )}
                    emptyLabel={t(
                      'agents.form.modelSearchEmpty',
                      'No models match',
                    )}
                    ariaLabel={`${t('agents.form.fallbackModels', 'Fallback models')} ${index + 1}`}
                    triggerClass="agents-view__dropdown"
                    panelClass="agents-view__search-panel"
                    onOpenChange={onModelDropdownOpenChange}
                    onValueChange={(selectedValue) =>
                      updateFallbackModelEntry(index, selectedValue)}
                  />
                  <button
                    type="button"
                    class="agents-view__fallback-remove"
                    aria-label={t(
                      'agents.form.removeFallbackModel',
                      'Remove fallback model',
                    )}
                    onclick={() => removeFallbackModelEntry(index)}
                  >
                    ×
                  </button>
                </div>
              {/each}
              {#if canAddFallbackModelRow}
                <button
                  type="button"
                  class="agents-view__fallback-add"
                  onclick={addFallbackModelEntry}
                >
                  {t('agents.form.addFallbackModel', '+ Add fallback model')}
                </button>
              {/if}
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</div>
