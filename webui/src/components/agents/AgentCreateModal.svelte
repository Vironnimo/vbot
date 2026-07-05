<script>
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Button from '../ui/Button.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import { rpc } from '$lib/api.js';
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
  let modelInheritLabel = $derived(inheritLabelForDefault('model'));
  let modelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: formValues.model,
      emptyLabel: modelInheritLabel,
      translate: t,
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
          : t(`agents.form.thinkingEffortOption.${option}`, option),
    })),
  );

  // The inherit-option label for a run field, derived from the global default
  // (the modal's stand-in for "effective" — no agent exists yet). A present
  // default → "Inherited: <value> (global default)"; absent → "Inherit (not
  // configured)".
  function inheritLabelForDefault(fieldName) {
    const value = defaultValueText(fieldName);
    if (value) {
      return t(
        'agents.form.inheritOption',
        'Inherited: {value} (global default)',
        {
          value,
        },
      );
    }
    return t(
      'agents.form.inheritOptionNotConfigured',
      'Inherit (not configured)',
    );
  }

  function thinkingEffortInheritLabel() {
    const value = defaultValueText('thinking_effort');
    if (value) {
      return t(
        'agents.form.inheritOption',
        'Inherited: {value} (global default)',
        {
          value,
        },
      );
    }
    return t(
      'agents.form.inheritOptionProviderDefault',
      'Inherit (provider default)',
    );
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
      errorMessage = t(
        'errors.validation',
        'Check the highlighted fields and try again.',
      );
      return;
    }

    isSaving = true;

    try {
      const savedAgent = await rpc('agent.create', result.payload);
      onToast({
        title: t('agents.created', 'Agent created.'),
        variant: 'success',
      });
      await onCreated(savedAgent.id ?? result.payload.id);
    } catch (error) {
      errorMessage =
        error?.message ||
        t('agents.saveError') ||
        t('errors.generic', 'Something went wrong. Try again.');
    } finally {
      isSaving = false;
    }
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
</script>

<Modal
  title={t('agents.create', 'Create agent')}
  labelledById="agent-create-modal-title"
  class="agents-view__create-modal"
  closeDisabled={isSaving}
  onClose={close}
>
  {#snippet body()}
    <form onsubmit={submit}>
      <div class="modal-body agents-view__create-modal-body">
        <label class="modal-field">
          <span class="modal-label">{t('agents.form.id', 'Agent ID')}</span>
          <TextField
            invalid={Boolean(formErrors.id)}
            value={formValues.id}
            disabled={isSaving}
            onInput={(next) => {
              formValues.id = next;
              formErrors.id = '';
              errorMessage = '';
            }}
          />
          {#if formErrors.id}
            <small class="agents-view__field-error">
              {fieldError('id')}
            </small>
          {/if}
        </label>

        <label class="modal-field">
          <span class="modal-label">{t('agents.form.name', 'Name')}</span>
          <TextField
            invalid={Boolean(formErrors.name)}
            value={formValues.name}
            disabled={isSaving}
            onInput={(next) => {
              formValues.name = next;
              formErrors.name = '';
              errorMessage = '';
            }}
          />
          {#if formErrors.name}
            <small class="agents-view__field-error">
              {fieldError('name')}
            </small>
          {/if}
        </label>

        <label class="modal-field">
          <span class="modal-label">{t('agents.form.model', 'Model')}</span>
          <SearchableDropdown
            id="agent-create-model"
            value={modelSelectValue}
            options={modelOptions}
            placeholder={modelInheritLabel}
            searchPlaceholder={t(
              'agents.form.modelSearchPlaceholder',
              'Filter models…',
            )}
            emptyLabel={t('agents.form.modelSearchEmpty', 'No models match')}
            ariaLabel={t('agents.form.model', 'Model')}
            disabled={isSaving}
            triggerClass="agents-view__dropdown"
            panelClass="agents-view__search-panel agents-view__modal-search-panel"
            onValueChange={updateModelSelection}
          />
        </label>

        <label class="modal-field">
          <span class="modal-label">
            {t('agents.form.thinkingEffort', 'Thinking effort')}
          </span>
          <Dropdown
            id="agent-create-thinking-effort"
            value={formValues.thinking_effort}
            options={thinkingEffortOptions}
            disabled={isSaving || effortDropdownDisabled}
            ariaLabel={t('agents.form.thinkingEffort', 'Thinking effort')}
            triggerClass="agents-view__dropdown"
            listClass="agents-view__thinking-list agents-view__modal-thinking-list"
            onValueChange={(selectedValue) => {
              formValues.thinking_effort = selectedValue;
            }}
          />
          {#if effortDropdownDisabled}
            <small
              class="agents-view__field-help"
              data-testid="create-thinking-effort-disabled-hint"
            >
              {t(
                'agents.form.thinkingEffortUnsupported',
                'This model does not support reasoning.',
              )}
            </small>
          {/if}
        </label>

        <label class="modal-field">
          <span class="modal-label">
            {t('agents.form.temperature', 'Temperature')}
          </span>
          <TextField
            inputmode="decimal"
            invalid={Boolean(formErrors.temperature)}
            value={formValues.temperature}
            disabled={isSaving}
            onInput={(next) => {
              formValues.temperature = next;
              formErrors.temperature = '';
              errorMessage = '';
            }}
          />
          {#if formErrors.temperature}
            <small class="agents-view__field-error">
              {fieldError('temperature')}
            </small>
          {/if}
        </label>

        {#if errorMessage}
          <p
            class="agents-view__notice agents-view__notice--error"
            role="alert"
          >
            {errorMessage}
          </p>
        {/if}
      </div>

      <div class="modal-footer">
        <Button variant="secondary" disabled={isSaving} onClick={close}>
          {t('common.cancel', 'Cancel')}
        </Button>
        <Button variant="primary" type="submit" disabled={isSaving}>
          {isSaving
            ? t('common.saving', 'Saving…')
            : t('agents.form.submitCreate', 'Create agent')}
        </Button>
      </div>
    </form>
  {/snippet}
</Modal>
