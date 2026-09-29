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
    modelSelectionParts,
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
    }),
  );

  let modelSelectValue = $derived(
    selectModelValue(formValues.model, modelOptions),
  );

  // The control column is narrow: the trigger's card splits the choice into
  // the complete Model id and its Connection, and says where an inherited
  // Model comes from.
  let modelTriggerTooltip = $derived(modelTriggerDetails(formValues.model));

  function modelTriggerDetails(value) {
    const inherited =
      !value && inheritSource('model') === 'global_default'
        ? inheritDisplayValue('model')
        : '';
    if (!value && !inherited) {
      return { text: t('agents.details.modelNotConfigured'), placement: 'top' };
    }
    const parts = modelSelectionParts(value || inherited, availableConnections);
    return {
      text: inherited ? t('agents.details.modelInherited') : '',
      rows: [
        { label: t('agents.form.model'), value: parts.model, mono: true },
        {
          label: t('agents.details.connection'),
          value:
            parts.connection ||
            (inherited ? '' : t('agents.details.anyConnection')),
        },
      ],
      placement: 'top',
    };
  }

  // The fallback chain binds to the raw ordered list. Every row shares one
  // unfiltered option catalog — a chain is short (max 5) and rows stay
  // comparable, so no per-row show/hide footer is needed.
  const MAX_FALLBACK_MODEL_ROWS = 5;

  let allFallbackModelOptions = $derived(
    selectModelOptions('', t('agents.form.fallbackModelInherit')),
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
        label: t('agents.form.noProject'),
      },
      ...(Array.isArray(projectOptions) ? projectOptions : []),
    ];
    const selected = formValues.root_project_id;
    if (selected && !options.some((option) => option.value === selected)) {
      options.push({
        value: selected,
        label: t('agents.form.unavailableProject'),
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
        return t('inherit.option', {
          value: inheritDisplayValue('thinking_effort'),
        });
      }
      return t('inherit.optionProviderDefault');
    }

    return t(`agents.form.thinkingEffortOption.${option}`);
  }

  // The empty-option label for the model / fallback-model select. Uses that
  // field's effective source: a global default fills the value; a fully
  // unconfigured model shows "Inherit (not configured)".
  function inheritModelLabel(fieldName) {
    if (inheritSource(fieldName) === 'global_default') {
      return t('inherit.option', {
        value: inheritDisplayValue(fieldName),
      });
    }
    return t('inherit.optionNotConfigured');
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
        {t('agents.detail.identity')}
      </h3>
    </header>
    <div class="s-section__body">
      <div class="s-group">
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-name">
              {t('agents.form.name')}
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
                {t('agents.form.project')}
              </label>
              <div class="s-row-desc" id="agent-project-help">
                {projectCatalogError
                  ? t('agents.form.projectUnavailableHelp')
                  : t('agents.form.projectHelp')}
              </div>
            </div>
            <div class="s-row-control">
              <Dropdown
                id="agent-project"
                value={formValues.root_project_id ?? ''}
                options={projectDropdownOptions}
                disabled={Boolean(projectCatalogError)}
                ariaLabel={t('agents.form.project')}
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
        {t('agents.detail.model')}
      </h3>
      {#if formMode === AGENT_FORM_MODE_EDIT}
        <div class="s-section__aside">
          <Button variant="tertiary" onClick={navigateToAgentDefaults}>
            {t('inherit.editGlobalDefaults')}
          </Button>
        </div>
      {/if}
    </header>
    <div class="s-section__body">
      <div class="s-group agents-view__model-group">
        <div class="s-row">
          <div class="s-row-info">
            <label class="s-row-label" for="agent-model">
              {t('agents.form.model')}
            </label>
          </div>
          <div class="s-row-control">
            <SearchableDropdown
              id="agent-model"
              value={modelSelectValue}
              options={modelOptions}
              placeholder={inheritModelLabel('model')}
              searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
              emptyLabel={t('agents.form.modelSearchEmpty')}
              ariaLabel={t('agents.form.model')}
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
              {t('agents.form.thinkingEffort')}
            </label>
            <div class="s-row-desc" id="agent-thinking-effort-help">
              {effortDropdownDisabled
                ? t('agents.form.thinkingEffortUnsupported')
                : t('agents.form.thinkingEffortDescription')}
            </div>
          </div>
          <div class="s-row-control">
            <Dropdown
              id="agent-thinking-effort"
              value={formValues.thinking_effort}
              options={thinkingEffortOptions}
              disabled={effortDropdownDisabled}
              ariaLabel={t('agents.form.thinkingEffort')}
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
            {t('agents.modelOptions')}
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
                {t('agents.form.temperature')}
              </label>
              <div class="s-row-desc" id="agent-temperature-desc">
                {t('agents.form.temperatureDescription')}
              </div>
              {#if temperatureIsInherit}
                <div
                  class="s-row-desc agents-view__inherit-hint"
                  id="agent-temperature-help"
                >
                  {inheritSource('temperature') === 'global_default'
                    ? t('inherit.hint', {
                        value: inheritDisplayValue('temperature'),
                      })
                    : t('inherit.hintProviderDefault')}
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
                  tooltip={t('inherit.resetToInherit')}
                  ariaLabel={t('inherit.resetToInherit')}
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
                {t('agents.form.fallbackModels')}
              </div>
              <div class="s-row-desc">
                {t('agents.form.fallbackModelsHelp')}
              </div>
            </div>
            <div class="s-row-control agents-view__fallback-list">
              {#each fallbackModelRows as row, index (index)}
                <div class="agents-view__fallback-row">
                  <SearchableDropdown
                    id={`agent-fallback-model-${index}`}
                    value={row.selectValue}
                    options={allFallbackModelOptions}
                    placeholder={t('agents.form.fallbackModelPlaceholder')}
                    searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
                    emptyLabel={t('agents.form.modelSearchEmpty')}
                    ariaLabel={`${t('agents.form.fallbackModels')} ${index + 1}`}
                    triggerClass="agents-view__dropdown"
                    panelClass="agents-view__search-panel"
                    onOpenChange={onModelDropdownOpenChange}
                    onValueChange={(selectedValue) =>
                      updateFallbackModelEntry(index, selectedValue)}
                  />
                  <button
                    type="button"
                    class="agents-view__fallback-remove"
                    aria-label={t('agents.form.removeFallbackModel')}
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
                  {t('agents.form.addFallbackModel')}
                </button>
              {/if}
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</div>
