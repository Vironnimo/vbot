<script>
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import { createAgent } from '$lib/api.js';
  import {
    AGENT_FORM_MODE_CREATE,
    createAgentFormValues,
    effortOptionsForReasoning,
    normalizeAgentForm,
    reasoningForModelValue,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';

  let {
    availableModels = [],
    availableConnections = [],
    // The global agent defaults (`settings.get` → `defaults.agent`), fetched by
    // AgentsView when the modal opens. Since no agent exists yet, the modal's
    // "effective" for a run field IS the global default directly: a present
    // default fills the inherit-option value; an absent one shows the
    // not-configured / provider-default label. Empty object on a fetch failure.
    agentDefaults = {},
    onCreated = async () => {},
    onClose = () => {},
    onToast = () => {},
  } = $props();

  let formValues = $state(createAgentFormValues());
  let formErrors = $state({});
  let errorMessage = $state('');
  let isSaving = $state(false);
  let showAllModels = $state(false);
  let modelInheritLabel = $derived(inheritLabelForDefault('model'));
  let allModelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: formValues.model,
      emptyLabel: modelInheritLabel,
    }),
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
  let selectedModelReasoning = $derived(
    reasoningForModelValue(formValues.model, availableModels),
  );
  let effortDropdownDisabled = $derived(
    selectedModelReasoning?.supported === false,
  );
  let thinkingEffortOptions = $derived(
    effortOptionsForReasoning(selectedModelReasoning).map((option) => ({
      value: option,
      label:
        option === ''
          ? thinkingEffortInheritLabel()
          : t(`agents.form.thinkingEffortOption.${option}`),
    })),
  );

  // The inherit-option label for a run field, derived from the global default
  // (the modal's stand-in for "effective" — no agent exists yet). A present
  // default → "Inherited: <value> (global default)"; absent → "Inherit (not
  // configured)".
  function inheritLabelForDefault(fieldName) {
    const value = defaultValueText(fieldName);
    if (value) {
      return t('inherit.option', {
        value,
      });
    }
    return t('inherit.optionNotConfigured');
  }

  function thinkingEffortInheritLabel() {
    const value = defaultValueText('thinking_effort');
    if (value) {
      return t('inherit.option', {
        value,
      });
    }
    return t('inherit.optionProviderDefault');
  }

  function defaultValueText(fieldName) {
    const raw =
      agentDefaults && typeof agentDefaults === 'object'
        ? agentDefaults[fieldName]
        : null;
    if (raw === null || raw === undefined) {
      return '';
    }
    return String(raw).trim();
  }

  function close() {
    if (!isSaving) {
      onClose();
    }
  }

  function updateModelSelection(selectedValue) {
    const selection = parseModelSelectionValue(selectedValue);
    formValues.model = modelSelectionValue(
      selection.model,
      selection.connectionLocalId,
    );
  }

  async function submit(event) {
    event.preventDefault();

    if (isSaving) {
      return;
    }

    const result = normalizeAgentForm(formValues, {
      mode: AGENT_FORM_MODE_CREATE,
    });

    formErrors = result.errors;
    errorMessage = '';

    if (!result.isValid) {
      errorMessage = t('errors.validation');
      return;
    }

    isSaving = true;

    try {
      const savedAgent = await createAgent(result.payload);
      onToast({
        title: t('agents.created'),
        variant: 'success',
      });
      await onCreated(savedAgent.id ?? result.payload.id);
    } catch (error) {
      errorMessage =
        error?.message || t('agents.saveError') || t('errors.generic');
    } finally {
      isSaving = false;
    }
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
</script>

<Modal
  title={t('agents.create')}
  labelledById="agent-create-modal-title"
  class="agents-view__create-modal"
  closeDisabled={isSaving}
  onClose={close}
>
  {#snippet body()}
    <form onsubmit={submit}>
      <div class="modal-body agents-view__create-modal-body">
        <FormField
          controlId="agent-create-id"
          label={t('agents.form.id')}
          required
          help={t('agents.form.idHelp')}
          error={formErrors.id ? fieldError('id') : ''}
        >
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              invalid={field.invalid}
              aria-describedby={field.describedBy}
              value={formValues.id}
              disabled={isSaving}
              onInput={(next) => {
                formValues.id = next;
                formErrors.id = '';
                errorMessage = '';
              }}
            />
          {/snippet}
        </FormField>

        <FormField
          controlId="agent-create-name"
          label={t('agents.form.name')}
          error={formErrors.name ? fieldError('name') : ''}
        >
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              invalid={field.invalid}
              aria-describedby={field.describedBy}
              value={formValues.name}
              disabled={isSaving}
              onInput={(next) => {
                formValues.name = next;
                formErrors.name = '';
                errorMessage = '';
              }}
            />
          {/snippet}
        </FormField>

        <FormField
          controlId="agent-create-model"
          label={t('agents.form.model')}
        >
          <SearchableDropdown
            id="agent-create-model"
            value={modelSelectValue}
            options={modelOptions}
            placeholder={modelInheritLabel}
            searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
            emptyLabel={t('agents.form.modelSearchEmpty')}
            ariaLabel={t('agents.form.model')}
            disabled={isSaving}
            triggerClass="agents-view__dropdown"
            panelClass="agents-view__search-panel agents-view__modal-search-panel"
            footerActionLabel={modelFilterFooter}
            onFooterAction={() => (showAllModels = !showAllModels)}
            onValueChange={updateModelSelection}
          />
        </FormField>

        <FormField
          controlId="agent-create-thinking-effort"
          help={effortDropdownDisabled
            ? t('agents.form.thinkingEffortUnsupported')
            : ''}
        >
          {#snippet labelContent()}
            {t('agents.form.thinkingEffort')}
            <InfoHint text={t('agents.form.thinkingEffortHelp')} />
          {/snippet}
          <Dropdown
            id="agent-create-thinking-effort"
            value={formValues.thinking_effort}
            options={thinkingEffortOptions}
            disabled={isSaving || effortDropdownDisabled}
            ariaLabel={t('agents.form.thinkingEffort')}
            triggerClass="agents-view__dropdown"
            listClass="agents-view__thinking-list agents-view__modal-thinking-list"
            onValueChange={(selectedValue) => {
              formValues.thinking_effort = selectedValue;
            }}
          />
        </FormField>

        <FormField
          controlId="agent-create-temperature"
          error={formErrors.temperature ? fieldError('temperature') : ''}
        >
          {#snippet labelContent()}
            {t('agents.form.temperature')}
            <InfoHint text={t('agents.form.temperatureHelp')} />
          {/snippet}
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              inputmode="decimal"
              invalid={field.invalid}
              aria-describedby={field.describedBy}
              value={formValues.temperature}
              disabled={isSaving}
              onInput={(next) => {
                formValues.temperature = next;
                formErrors.temperature = '';
                errorMessage = '';
              }}
            />
          {/snippet}
        </FormField>

        {#if errorMessage}
          <Banner variant="error" role="alert">
            {errorMessage}
          </Banner>
        {/if}
      </div>

      <div class="modal-footer">
        <Button variant="secondary" disabled={isSaving} onClick={close}>
          {t('common.cancel')}
        </Button>
        <Button variant="primary" type="submit" disabled={isSaving}>
          {isSaving ? t('common.saving') : t('agents.form.submitCreate')}
        </Button>
      </div>
    </form>
  {/snippet}
</Modal>
