<script>
  import { untrack } from 'svelte';
  import { isChatAgent } from '$lib/agentPlacement.js';
  import Dropdown from '../Dropdown.svelte';
  import SearchableDropdown from '../SearchableDropdown.svelte';
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import InfoHint from '../ui/InfoHint.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import PermissionProfilePicker from '../permissions/PermissionProfilePicker.svelte';
  import { createAgent } from '$lib/api.js';
  import {
    AGENT_DESCRIPTION_MAX_LENGTH,
    AGENT_FORM_MODE_CREATE,
    createAgentFormValues,
    effortOptionsForReasoning,
    effortForReasoning,
    normalizeAgentForm,
    reasoningForModelValue,
    THINKING_EFFORT_OPTIONS,
  } from '$lib/agentForm.js';
  import {
    defaultChoiceValue,
    defaultOptionLabel,
    markDefaultOption,
  } from '$lib/defaultChoice.js';
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
    agents = [],
    initialPlacement = { kind: 'chat' },
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

  let formValues = $state(
    createAgentFormValues({ placement: untrack(() => initialPlacement) }),
  );
  let ownerOptions = $derived(
    agents
      .filter(isChatAgent)
      .map((agent) => ({ value: agent.id, label: agent.name || agent.id })),
  );
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
  // The global default effort is the level marked "(default)"; with none set
  // the Provider decides.
  let defaultThinkingEffort = $derived(defaultValueText('thinking_effort'));
  let thinkingEffortOptions = $derived(
    markDefaultOption(
      effortOptionsForReasoning(selectedModelReasoning)
        .filter(Boolean)
        .map((option) => ({ value: option, label: effortLabel(option) })),
      {
        defaultValue: defaultThinkingEffort,
        fallbackLabel: defaultThinkingEffort
          ? defaultOptionLabel(effortLabel(defaultThinkingEffort))
          : t('inherit.optionProviderDefault'),
      },
    ),
  );
  let thinkingEffortValue = $derived(
    defaultChoiceValue(formValues.thinking_effort, {
      defaultValue: defaultThinkingEffort,
    }),
  );

  // The inherit-option label for a run field, derived from the global default
  // (the modal's stand-in for "effective" — no agent exists yet). A present
  // default → "<value> (default)"; absent → "Inherit (not configured)".
  function inheritLabelForDefault(fieldName) {
    const value = defaultValueText(fieldName);
    return value ? defaultOptionLabel(value) : t('inherit.optionNotConfigured');
  }

  function effortLabel(option) {
    return THINKING_EFFORT_OPTIONS.includes(option)
      ? t(`agents.form.thinkingEffortOption.${option}`)
      : option;
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
    formValues.thinking_effort = effortForReasoning(
      formValues.thinking_effort,
      reasoningForModelValue(
        formValues.model || defaultValueText('model'),
        availableModels,
      ),
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
    if (formErrors[fieldName] === 'invalid_id') {
      return t('agents.rename.invalidId');
    }
    if (formErrors[fieldName] === 'too_long') {
      return t('agents.form.descriptionTooLong', {
        max: AGENT_DESCRIPTION_MAX_LENGTH,
      });
    }
    return t('errors.validation');
  }
</script>

<Modal
  title={t('agents.create')}
  labelledById="agent-create-modal-title"
  size="md"
  closeDisabled={isSaving}
  onClose={close}
>
  {#snippet body()}
    <form onsubmit={submit}>
      <div class="modal-body">
        <FormField
          controlId="agent-create-id"
          label={t('agents.form.id')}
          required
          help={t('agents.form.idCreateHelp')}
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
          controlId="agent-create-placement"
          label={t('agents.placement.title')}
        >
          {#snippet children(field)}
            <Dropdown
              id={field.controlId}
              ariaLabel={t('agents.placement.title')}
              ariaLabelledby={field.labelId}
              value={formValues.placement.kind}
              options={[
                { value: 'chat', label: t('agents.placement.chat') },
                { value: 'helper', label: t('agents.placement.helper') },
                {
                  value: 'private',
                  label: t('agents.placement.private'),
                  disabled: ownerOptions.length === 0,
                },
              ]}
              onValueChange={(kind) =>
                (formValues.placement =
                  kind === 'private'
                    ? { kind, owner_id: ownerOptions[0]?.value ?? '' }
                    : { kind })}
            />
          {/snippet}
        </FormField>
        {#if formValues.placement.kind === 'private'}
          <FormField
            controlId="agent-create-owner"
            label={t('agents.placement.owner')}
            error={formErrors.placement ? fieldError('placement') : ''}
          >
            {#snippet children(field)}
              <Dropdown
                id={field.controlId}
                ariaLabel={t('agents.placement.owner')}
                ariaLabelledby={field.labelId}
                ariaDescribedby={field.describedBy}
                invalid={field.invalid}
                value={formValues.placement.owner_id}
                options={ownerOptions}
                onValueChange={(owner_id) =>
                  (formValues.placement = { kind: 'private', owner_id })}
              />
            {/snippet}
          </FormField>
        {/if}

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
          controlId="agent-create-description"
          label={t('agents.form.description')}
          help={t('agents.form.descriptionHelp')}
          error={formErrors.description ? fieldError('description') : ''}
        >
          {#snippet children(field)}
            <TextField
              id={field.controlId}
              invalid={field.invalid}
              aria-describedby={field.describedBy}
              value={formValues.description}
              placeholder={t('agents.form.descriptionPlaceholder')}
              maxlength={AGENT_DESCRIPTION_MAX_LENGTH}
              disabled={isSaving}
              onInput={(next) => {
                formValues.description = next;
                formErrors.description = '';
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
            value={thinkingEffortValue}
            options={thinkingEffortOptions}
            disabled={isSaving || effortDropdownDisabled}
            ariaLabel={t('agents.form.thinkingEffort')}
            triggerClass="agents-view__dropdown"
            onValueChange={(selectedValue) => {
              formValues.thinking_effort = selectedValue;
            }}
          />
        </FormField>

        <FormField
          controlId="agent-create-permissions-profile"
          help={t('permissions.createHelp')}
        >
          {#snippet labelContent()}
            {t('permissions.title')}
            <InfoHint text={t('permissions.agentDescription')} />
          {/snippet}
          <PermissionProfilePicker
            profile={formValues.permission_profile}
            rules={formValues.permission_rules}
            inherited={agentDefaults?.permission_rules ?? null}
            compact
            disabled={isSaving}
            idPrefix="agent-create-permissions"
            onChange={(profile, rules) => {
              formValues.permission_profile = profile;
              formValues.permission_rules = rules;
            }}
          />
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
