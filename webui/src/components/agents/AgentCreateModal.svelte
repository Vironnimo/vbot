<script>
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import { rpc } from '$lib/api.js';
  import {
    AGENT_FORM_MODE_CREATE,
    createAgentFormValues,
    normalizeAgentForm,
  } from '$lib/agentForm.js';
  import { t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    modelSelectionValue,
    parseModelSelectionValue,
    selectModelValue,
  } from '$lib/modelSelection.js';

  const EMPTY_VALUE = '—';
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
    availableModels = [],
    availableConnections = [],
    onCreated = async () => {},
    onClose = () => {},
    onToast = () => {},
  } = $props();

  let formValues = $state(createAgentFormValues());
  let formErrors = $state({});
  let errorMessage = $state('');
  let isSaving = $state(false);
  let modelOptions = $derived(
    buildModelSelectOptions({
      models: availableModels,
      connections: availableConnections,
      selectedModelValue: formValues.model,
      emptyLabel: t(
        'agents.form.modelPlaceholder',
        'Default (no model selected)',
      ),
      translate: t,
    }),
  );
  let modelSelectValue = $derived(
    selectModelValue(formValues.model, modelOptions),
  );
  let thinkingEffortOptions = $derived(
    THINKING_EFFORT_OPTIONS.map((option) => ({
      value: option,
      label:
        option === ''
          ? t('agents.form.thinkingEffortDefault', EMPTY_VALUE)
          : t(`agents.form.thinkingEffortOption.${option}`, option),
    })),
  );

  function close() {
    if (!isSaving) {
      onClose();
    }
  }

  function handleDocumentKeydown(event) {
    if (event.key === 'Escape') {
      close();
    }
  }

  function handleOverlayClick(event) {
    if (event.target === event.currentTarget) {
      close();
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

<svelte:document onkeydown={handleDocumentKeydown} />

<div
  class="modal-overlay open"
  role="presentation"
  onclick={handleOverlayClick}
>
  <div
    class="modal agents-view__create-modal"
    role="dialog"
    aria-modal="true"
    aria-labelledby="agent-create-modal-title"
  >
    <div class="modal-header">
      <h3 id="agent-create-modal-title" class="modal-title">
        {t('agents.create', 'Create agent')}
      </h3>
      <button
        type="button"
        class="modal-close"
        aria-label={t('common.close', 'Close')}
        disabled={isSaving}
        onclick={close}
      >
        ×
      </button>
    </div>

    <form onsubmit={submit}>
      <div class="modal-body agents-view__create-modal-body">
        <label class="modal-field">
          <span class="modal-label">{t('agents.form.id', 'Agent ID')}</span>
          <input
            class:agents-view__invalid={formErrors.id}
            class="s-input"
            type="text"
            value={formValues.id}
            disabled={isSaving}
            oninput={(event) => {
              formValues.id = event.currentTarget.value;
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
          <input
            class:agents-view__invalid={formErrors.name}
            class="s-input"
            type="text"
            value={formValues.name}
            disabled={isSaving}
            oninput={(event) => {
              formValues.name = event.currentTarget.value;
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
            ariaLabel={t('agents.form.thinkingEffort', 'Thinking effort')}
            disabled={isSaving}
            triggerClass="agents-view__dropdown"
            listClass="agents-view__thinking-list agents-view__modal-thinking-list"
            onValueChange={(selectedValue) => {
              formValues.thinking_effort = selectedValue;
            }}
          />
        </label>

        <label class="modal-field">
          <span class="modal-label">
            {t('agents.form.temperature', 'Temperature')}
          </span>
          <input
            class:agents-view__invalid={formErrors.temperature}
            class="s-input"
            type="text"
            inputmode="decimal"
            value={formValues.temperature}
            disabled={isSaving}
            oninput={(event) => {
              formValues.temperature = event.currentTarget.value;
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
        <button
          type="button"
          class="modal-btn-cancel"
          disabled={isSaving}
          onclick={close}
        >
          {t('common.cancel', 'Cancel')}
        </button>
        <button type="submit" class="modal-btn-confirm" disabled={isSaving}>
          {isSaving
            ? t('common.saving', 'Saving…')
            : t('agents.form.submitCreate', 'Create agent')}
        </button>
      </div>
    </form>
  </div>
</div>
