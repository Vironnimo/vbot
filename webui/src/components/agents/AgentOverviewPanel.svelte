<script>
  import { t } from '$lib/i18n.js';
  import {
    AGENT_FORM_MODE_EDIT,
    reasoningForModelValue,
    effortOptionsForReasoning,
  } from '$lib/agentForm.js';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
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
    activeDetail,
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

  function clearTemperature() {
    formValues.temperature = '';
  }
</script>

<div
  class="management-topic"
  role="tabpanel"
  id="agent-detail-panel-overview"
  aria-labelledby="agent-detail-tab-overview"
  hidden={activeDetail !== 'overview'}
  tabindex="0"
>
  <div class="detail-group agents-view__model-group">
    <div class="detail-group-title agents-view__group-title-row">
      <span>{t('agents.detail.model', 'Model')}</span>
      {#if formMode === AGENT_FORM_MODE_EDIT}
        <Button
          variant="tertiary"
          class="agents-view__inherit-link"
          onClick={navigateToAgentDefaults}
        >
          {t('inherit.editGlobalDefaults', 'Edit global defaults')}
        </Button>
      {/if}
    </div>
    <div class="detail-fields agents-view__model-fields">
      <FormField
        controlId="agent-model"
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
      </FormField>

      <FormField controlId="agent-fallback-models" full>
        {#snippet labelContent()}
          {t('agents.form.fallbackModels', 'Fallback models')}
          <InfoHint
            text={t(
              'agents.form.fallbackModelsHelp',
              'Tried in order when the primary model fails or is unavailable. The first entry has the highest priority.',
            )}
          />
        {/snippet}
        {#each fallbackModelRows as row, index (index)}
          <div class="agents-view__fallback-row">
            <SearchableDropdown
              id={`agent-fallback-model-${index}`}
              value={row.selectValue}
              options={allFallbackModelOptions}
              placeholder={t('agents.form.fallbackModelPlaceholder', 'None')}
              searchPlaceholder={t(
                'agents.form.modelSearchPlaceholder',
                'Filter models…',
              )}
              emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
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
      </FormField>
    </div>
  </div>
  <div class="detail-group">
    <div class="detail-group-title">
      {t('agents.detail.identity', 'Identity')}
    </div>
    <div class="detail-fields">
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

      {#if formMode === AGENT_FORM_MODE_EDIT}
        <FormField
          controlId="agent-project"
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
</div>
