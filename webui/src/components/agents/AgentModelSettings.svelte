<script>
  // How an Agent runs: its Model with the thinking effort, and behind a
  // disclosure the temperature and the fallback chain. It edits the bound
  // Agent form values (`createAgentFormValues`), and an empty field shows
  // what it inherits (`inheritSource`/`inheritDisplayValue` read the Agent's
  // `effective` block). An Agent's page and the Librarian's Settings panel
  // both use it; `idPrefix` keeps their element ids apart.
  import { t } from '$lib/i18n.js';
  import {
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

  const noop = () => {};

  let {
    availableModels = [],
    availableConnections = [],
    formValues = $bindable(),
    formErrors = {},
    fieldError = noop,
    inheritSource,
    inheritDisplayValue,
    onModelDropdownOpenChange = noop,
    idPrefix = 'agent',
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
      `${idPrefix}-temperature-desc`,
      temperatureIsInherit ? `${idPrefix}-temperature-help` : '',
      formErrors.temperature ? `${idPrefix}-temperature-error` : '',
    ]
      .filter(Boolean)
      .join(' '),
  );

  function selectModelOptions(selectedModelValue, emptyLabel) {
    return buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue,
      emptyLabel,
    });
  }

  function updateModelSelection(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    formValues.model = modelSelectionValue(
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

<div class="s-group agent-model-settings">
  <div class="s-row">
    <div class="s-row-info">
      <label class="s-row-label" for={`${idPrefix}-model`}>
        {t('agents.form.model')}
      </label>
    </div>
    <div class="s-row-control">
      <SearchableDropdown
        id={`${idPrefix}-model`}
        value={modelSelectValue}
        options={modelOptions}
        placeholder={inheritModelLabel('model')}
        searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
        emptyLabel={t('agents.form.modelSearchEmpty')}
        ariaLabel={t('agents.form.model')}
        triggerClass="agent-model-settings__dropdown"
        triggerTooltip={modelTriggerTooltip}
        footerActionLabel={modelFilterFooter}
        onFooterAction={() => (showAllModels = !showAllModels)}
        onOpenChange={onModelDropdownOpenChange}
        onValueChange={updateModelSelection}
      />
    </div>
  </div>

  <div class="s-row">
    <div class="s-row-info">
      <label class="s-row-label" for={`${idPrefix}-thinking-effort`}>
        {t('agents.form.thinkingEffort')}
      </label>
      <div class="s-row-desc" id={`${idPrefix}-thinking-effort-help`}>
        {effortDropdownDisabled
          ? t('agents.form.thinkingEffortUnsupported')
          : t('agents.form.thinkingEffortDescription')}
      </div>
    </div>
    <div class="s-row-control">
      <Dropdown
        id={`${idPrefix}-thinking-effort`}
        value={formValues.thinking_effort}
        options={thinkingEffortOptions}
        disabled={effortDropdownDisabled}
        ariaLabel={t('agents.form.thinkingEffort')}
        ariaDescribedby={`${idPrefix}-thinking-effort-help`}
        triggerClass="agent-model-settings__dropdown"
        listClass="agent-model-settings__thinking-list"
        onValueChange={(selectedValue) => {
          formValues.thinking_effort = selectedValue;
        }}
      />
    </div>
  </div>

  <button
    type="button"
    class="s-row s-row--compact s-disclosure-row"
    id={`${idPrefix}-model-options-toggle`}
    aria-expanded={modelOptionsOpen}
    aria-controls={`${idPrefix}-model-options`}
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
    id={`${idPrefix}-model-options`}
    hidden={!modelOptionsOpen}
  >
    <div class="s-row">
      <div class="s-row-info">
        <label class="s-row-label" for={`${idPrefix}-temperature`}>
          {t('agents.form.temperature')}
        </label>
        <div class="s-row-desc" id={`${idPrefix}-temperature-desc`}>
          {t('agents.form.temperatureDescription')}
        </div>
        {#if temperatureIsInherit}
          <div
            class="s-row-desc agent-model-settings__inherit-hint"
            id={`${idPrefix}-temperature-help`}
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
            class="agent-model-settings__error"
            id={`${idPrefix}-temperature-error`}
            role="alert"
          >
            {fieldError('temperature')}
          </p>
        {/if}
      </div>
      <div class="s-row-control agent-model-settings__temperature-control">
        <TextField
          id={`${idPrefix}-temperature`}
          inputmode="decimal"
          invalid={Boolean(formErrors.temperature)}
          aria-describedby={temperatureDescribedBy}
          value={formValues.temperature}
          onInput={(next) => (formValues.temperature = next)}
        />
        {#if !temperatureIsInherit}
          <Button
            variant="tertiary"
            class="agent-model-settings__reset-inherit"
            tooltip={t('inherit.resetToInherit')}
            ariaLabel={t('inherit.resetToInherit')}
            onClick={clearTemperature}
          >
            {EMPTY_VALUE}
          </Button>
        {/if}
      </div>
    </div>

    <div class="s-row agent-model-settings__fallbacks">
      <div class="s-row-info">
        <div class="s-row-label" id={`${idPrefix}-fallback-models-label`}>
          {t('agents.form.fallbackModels')}
        </div>
        <div class="s-row-desc">
          {t('agents.form.fallbackModelsHelp')}
        </div>
      </div>
      <div class="s-row-control agent-model-settings__fallback-list">
        {#each fallbackModelRows as row, index (index)}
          <div class="agent-model-settings__fallback-row">
            <SearchableDropdown
              id={`${idPrefix}-fallback-model-${index}`}
              value={row.selectValue}
              options={allFallbackModelOptions}
              placeholder={t('agents.form.fallbackModelPlaceholder')}
              searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
              emptyLabel={t('agents.form.modelSearchEmpty')}
              ariaLabel={`${t('agents.form.fallbackModels')} ${index + 1}`}
              triggerClass="agent-model-settings__dropdown"
              onOpenChange={onModelDropdownOpenChange}
              onValueChange={(selectedValue) =>
                updateFallbackModelEntry(index, selectedValue)}
            />
            <button
              type="button"
              class="agent-model-settings__fallback-remove"
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
            class="agent-model-settings__fallback-add"
            onclick={addFallbackModelEntry}
          >
            {t('agents.form.addFallbackModel')}
          </button>
        {/if}
      </div>
    </div>
  </div>
</div>
