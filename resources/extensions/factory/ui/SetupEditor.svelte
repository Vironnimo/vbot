<script>
  import { untrack } from 'svelte';
  import { scheduleAutosave } from '../../../../webui/src/lib/autosave.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import SaveStatus from '../../../../webui/src/components/ui/SaveStatus.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
  } from '../../../../webui/src/lib/modelSelection.js';
  import {
    effortOptionsForReasoning,
    effortForReasoning,
    reasoningForModelValue,
  } from '../../../../webui/src/lib/agentForm.js';
  import { createRecordEditor } from './recordEditor.svelte.js';
  import { setupDefaults, failureText } from './presentation.js';

  let { project, model, setup = null } = $props();
  let deleteOpen = $state(false);
  let showAllModels = $state(false);
  let previewed = $state(null);
  let previewError = $state('');
  const projectId = untrack(() => project.id);
  const fields = Object.keys(setupDefaults(projectId));
  const projectFields = (record) =>
    Object.fromEntries(
      fields.map((field) => [
        field,
        record[field] ?? setupDefaults(projectId)[field],
      ]),
    );
  const normalize = (draft) => ({
    ...draft,
    enabled: Boolean(draft.enabled),
    review_rounds: Number(draft.review_rounds),
    turn_budget: Number(draft.turn_budget),
    base_branch: draft.base_branch.trim(),
  });
  const editor = createRecordEditor({
    identity: projectId,
    client: untrack(() => model.client),
    get record() {
      return setup;
    },
    defaults: setupDefaults(projectId),
    project: projectFields,
    normalize,
    validate(draft) {
      const within = (value, maximum) =>
        Number.isInteger(Number(value)) &&
        Number(value) >= 1 &&
        Number(value) <= maximum;
      if (!draft.worker_profile)
        return t('factory.invalid.agent', { role: t('factory.worker') });
      if (!draft.reviewer_profile)
        return t('factory.invalid.agent', { role: reviewerLabel() });
      if (!within(draft.review_rounds, 20))
        return t('factory.invalid.reviewRounds');
      if (!within(draft.turn_budget, 1000))
        return t('factory.invalid.turnBudget');
      if (!draft.base_branch.trim() || draft.base_branch.trim().startsWith('-'))
        return t('factory.error.invalidBase');
      return '';
    },
    save: async (values, revision) => {
      const result = await model.call('setup.update', {
        ...values,
        expected_revision: revision,
      });
      model.acceptSavedSetup(result.setup, result.effective_models);
      return result.setup;
    },
    read: async () => {
      const result = await model.call('setup.get', { project_id: projectId });
      model.acceptSavedSetup(result.setup, result.effective_models);
      return result.setup;
    },
    onSave: (saved) => model.acceptSavedSetup(saved),
  });
  function reviewerLabel() {
    return t('factory.reviewer', { round: '' }).trim();
  }
  const roles = $derived([
    {
      id: 'worker',
      label: t('factory.worker'),
      source: 'worker_source',
      profile: 'worker_profile',
      model: 'worker_model',
      effort: 'worker_effort',
      empty: t('factory.inheritModel'),
    },
    {
      id: 'reviewer',
      label: reviewerLabel(),
      source: 'reviewer_source',
      profile: 'reviewer_profile',
      model: 'reviewer_model',
      effort: 'reviewer_effort',
      empty: t('factory.inheritModel'),
    },
  ]);
  const agents = $derived(model.catalog.agents ?? []);
  const team = $derived(project.agent_profiles ?? []);
  const models = $derived(model.catalog.models ?? []);
  const previewChoices = $derived({
    project_id: projectId,
    worker_source: editor.draft.worker_source,
    worker_profile: editor.draft.worker_profile,
    worker_model: editor.draft.worker_model,
    worker_effort: editor.draft.worker_effort,
    reviewer_source: editor.draft.reviewer_source,
    reviewer_profile: editor.draft.reviewer_profile,
    reviewer_model: editor.draft.reviewer_model,
    reviewer_effort: editor.draft.reviewer_effort,
  });
  const previewKey = $derived(JSON.stringify(previewChoices));
  // Existing configurations get the effective Models from their versioned save.
  // Only a new inherited selection needs a read-only, debounced preview.
  $effect(() => {
    const key = previewKey;
    const choices = { ...previewChoices };
    const saved = model.selectedSetup;
    const known =
      saved &&
      Object.entries(choices).every(
        ([field, value]) => saved[field] === value,
      ) &&
      model.setupEffectiveModels.worker &&
      model.setupEffectiveModels.reviewer;
    if (
      editor.exists ||
      known ||
      !choices.worker_profile ||
      !choices.reviewer_profile ||
      (choices.worker_model && choices.reviewer_model)
    )
      return;
    let cancelled = false;
    const cancel = scheduleAutosave(async () => {
      try {
        const result = await model.call('setup.preview', choices);
        if (!cancelled) {
          previewed = { key, models: result.effective_models };
          previewError = '';
        }
      } catch (cause) {
        if (!cancelled) previewError = failureText(cause);
      }
    });
    return () => {
      cancelled = true;
      cancel();
    };
  });
  // The role's Model, '' when its Agent resolves none, undefined while unknown.
  function resolvedRoleModel(role) {
    const saved = model.selectedSetup;
    if (editor.draft[`${role}_model`]) return editor.draft[`${role}_model`];
    return saved &&
      editor.draft[`${role}_source`] === saved[`${role}_source`] &&
      editor.draft[`${role}_profile`] === saved[`${role}_profile`] &&
      editor.draft[`${role}_model`] === saved[`${role}_model`]
      ? model.setupEffectiveModels[role]
      : previewed?.key === previewKey
        ? previewed.models[role]
        : undefined;
  }
  const effectiveRoleModel = (role) => resolvedRoleModel(role) || '';
  const modelUnresolved = (role) =>
    Boolean(editor.draft[`${role}_profile`]) && resolvedRoleModel(role) === '';
  const workerModel = $derived(effectiveRoleModel('worker'));
  const reviewerModel = $derived(effectiveRoleModel('reviewer'));
  const sameModel = $derived(
    Boolean(workerModel && reviewerModel && workerModel === reviewerModel),
  );
  const integrationOptions = $derived([
    { value: 'merged', label: t('factory.checkMerged') },
    { value: 'reference_only', label: t('factory.checkReference') },
  ]);

  // One picker per role over every Agent: the Identity Agents, then the
  // Project's Team. A value is `<source>:<id>`, split into the two fields.
  const profileValue = (role) =>
    editor.draft[role.profile]
      ? `${editor.draft[role.source]}:${editor.draft[role.profile]}`
      : '';

  function profileOptions(role) {
    const selected = profileValue(role);
    const options = [
      { value: '', label: t('factory.selectAgent') },
      ...agents.map((agent) => ({
        value: `agent:${agent.id}`,
        label: agent.name,
        secondaryLabel: agent.description,
        group: t('factory.agentsGroup'),
      })),
      ...team.map((profile) => ({
        value: `team:${profile.id}`,
        label: profile.name,
        group: t('factory.teamGroup', { project: project.name }),
      })),
    ];
    if (selected && !options.some((option) => option.value === selected))
      options.push({
        value: selected,
        label: editor.draft[role.profile],
        secondaryLabel: t('factory.agentUnavailable'),
        disabled: true,
      });
    return options;
  }

  function selectProfile(role, value) {
    const separator = value.indexOf(':');
    editor.draft[role.source] =
      separator > 0 ? value.slice(0, separator) : editor.draft[role.source];
    editor.draft[role.profile] =
      separator > 0 ? value.slice(separator + 1) : '';
  }

  function modelOptions(role) {
    return buildModelSelectOptions({
      models,
      modelOnly: true,
      selectedModelValue: editor.draft[role.model],
      emptyLabel: role.empty,
    });
  }

  function effectiveModel(role) {
    return effectiveRoleModel(role.id) || '';
  }

  function effortOptions(role) {
    return effortOptionsForReasoning(
      reasoningForModelValue(effectiveModel(role), models),
    ).map((value) => ({
      value,
      label:
        value === ''
          ? t('factory.defaultEffort')
          : t(`agents.form.thinkingEffortOption.${value}`),
    }));
  }

  function selectModel(role, value) {
    editor.draft[role.model] = value;
    editor.draft[role.effort] = effortForReasoning(
      editor.draft[role.effort],
      reasoningForModelValue(effectiveModel(role), models),
    );
  }

  async function deleteSetup() {
    if (!(await editor.flush())) return;
    if (await model.mutate('setup.delete', { project_id: projectId }))
      deleteOpen = false;
  }
</script>

<article class="factory-setup-editor" data-project-id={projectId}>
  <header class="factory-editor-header">
    <h2>{project.name}</h2>
    <p class="factory-help">{t('factory.setupHelp')}</p>
  </header>
  <section class="factory-section">
    <div class="factory-enable-row">
      <span id="factory-setup-enabled-label">{t('factory.enableSetup')}</span
      ><Toggle
        checked={Boolean(editor.draft.enabled)}
        ariaLabel={t('factory.enableSetup')}
        onChange={(value) => (editor.draft.enabled = value)}
      />
    </div>
    <p class="factory-help">{t('factory.enableSetupHelp')}</p>
  </section>
  {#each roles as role (role.id)}
    {@const allOptions = modelOptions(role)}
    {@const visibleOptions = filterModelSelectOptions(allOptions, {
      showAll: showAllModels,
      selectedModelValue: editor.draft[role.model],
    })}
    <section
      class="factory-section"
      aria-labelledby={`factory-setup-${role.id}`}
    >
      <h3 id={`factory-setup-${role.id}`}>{role.label}</h3>
      <div class="factory-fields">
        <div class="factory-field factory-field--wide">
          <label
            id={`factory-label-${role.profile}`}
            for={`factory-${role.profile}`}>{t('factory.agent')}</label
          >
          <Dropdown
            id={`factory-${role.profile}`}
            ariaLabelledby={`factory-label-${role.profile}`}
            value={profileValue(role)}
            options={profileOptions(role)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => selectProfile(role, value)}
          />
        </div>
        <div class="factory-field">
          <label
            id={`factory-label-${role.model}`}
            for={`factory-${role.model}`}>{t('factory.model')}</label
          >
          <SearchableDropdown
            id={`factory-${role.model}`}
            ariaLabelledby={`factory-label-${role.model}`}
            value={editor.draft[role.model]}
            options={visibleOptions}
            placeholder={role.empty}
            searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
            emptyLabel={t('agents.form.modelSearchEmpty')}
            footerActionLabel={modelFilterFooterLabel({
              showAll: showAllModels,
              hiddenCount: allOptions.length - visibleOptions.length,
            })}
            onFooterAction={() => (showAllModels = !showAllModels)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => selectModel(role, value)}
          />
          {#if modelUnresolved(role.id)}<p
              class="factory-field-error"
              role="status"
            >
              {t('factory.modelUnresolved')}
            </p>{/if}
        </div>
        <div class="factory-field">
          <label
            id={`factory-label-${role.effort}`}
            for={`factory-${role.effort}`}>{t('factory.effort')}</label
          >
          <Dropdown
            id={`factory-${role.effort}`}
            ariaLabelledby={`factory-label-${role.effort}`}
            value={editor.draft[role.effort]}
            options={effortOptions(role)}
            onValueChange={(value) => (editor.draft[role.effort] = value)}
          />
        </div>
      </div>
    </section>
  {/each}
  {#if sameModel}<Banner variant="warn" role="status"
      >{t('factory.sameModel')}</Banner
    >{/if}
  {#if previewError}<Banner variant="error" role="alert">{previewError}</Banner
    >{/if}
  <section class="factory-section" aria-labelledby="factory-setup-execution">
    <h3 id="factory-setup-execution">{t('factory.execution')}</h3>
    <div class="factory-fields">
      <div class="factory-field">
        <label for="factory-review-rounds">{t('factory.reviewRounds')}</label
        ><TextField
          id="factory-review-rounds"
          type="number"
          min="1"
          max="20"
          step="1"
          aria-describedby="factory-review-rounds-help"
          value={editor.draft.review_rounds}
          onInput={(value) => (editor.draft.review_rounds = value)}
        />
        <p id="factory-review-rounds-help" class="factory-help">
          {t('factory.reviewRoundsHelp')}
        </p>
      </div>
      <div class="factory-field">
        <label for="factory-turn-budget">{t('factory.turnBudget')}</label
        ><TextField
          id="factory-turn-budget"
          type="number"
          min="1"
          max="1000"
          step="1"
          aria-describedby="factory-turn-budget-help"
          value={editor.draft.turn_budget}
          onInput={(value) => (editor.draft.turn_budget = value)}
        />
        <p id="factory-turn-budget-help" class="factory-help">
          {t('factory.turnBudgetHelp')}
        </p>
      </div>
      <div class="factory-field">
        <label for="factory-base-branch">{t('factory.baseBranch')}</label
        ><TextField
          id="factory-base-branch"
          code
          value={editor.draft.base_branch}
          onInput={(value) => (editor.draft.base_branch = value)}
        />
      </div>
      <div class="factory-field">
        <label id="factory-integration-label" for="factory-integration-check"
          >{t('factory.integrationCheck')}</label
        ><Dropdown
          id="factory-integration-check"
          ariaLabelledby="factory-integration-label"
          value={editor.draft.integration_check}
          options={integrationOptions}
          onValueChange={(value) => (editor.draft.integration_check = value)}
        />
      </div>
    </div>
  </section>
  {#if editor.notice}<Banner variant="warn" role="status"
      >{editor.notice}</Banner
    >{/if}
  {#if editor.error}<Banner variant="error" role="alert">{editor.error}</Banner
    >{/if}
  <div class="factory-editor-footer">
    {#if editor.exists}<Button
        variant="tertiary"
        disabled={editor.saving || Boolean(model.pending)}
        onClick={() => (deleteOpen = true)}>{t('factory.deleteSetup')}</Button
      >{/if}
    <div class="factory-save-status">
      {#if editor.exists}<SaveStatus
          saving={editor.saving}
          pending={editor.hasChanges()}
          onClick={editor.save}
        />{:else}<Button
          variant="primary"
          loading={editor.saving}
          onClick={editor.save}>{t('factory.createSetup')}</Button
        >{/if}
    </div>
  </div>
</article>

{#if deleteOpen}
  <ConfirmDialog
    title={t('factory.deleteSetupTitle', { project: project.name })}
    body={t('factory.deleteSetupHelp')}
    confirmLabel={t('factory.deleteSetup')}
    busy={Boolean(model.pending)}
    error={model.error}
    onConfirm={deleteSetup}
    onCancel={() => (deleteOpen = false)}
  />
{/if}

<style>
  .factory-editor-header {
    margin-bottom: 24px;
  }
  .factory-editor-header h2 {
    margin: 0 0 10px;
    font-size: 20px;
  }
  .factory-enable-row {
    display: flex;
    gap: 18px;
    justify-content: space-between;
    align-items: center;
  }
  .factory-editor-footer {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-top: 20px;
  }
  .factory-save-status {
    margin-left: auto;
  }
  .factory-field--wide {
    grid-column: 1 / -1;
  }
  .factory-field-error {
    margin: 0;
    color: var(--red);
    font-size: 12px;
    line-height: 1.5;
  }
  .factory-setup-editor > :global(.banner) {
    margin-bottom: 18px;
  }
</style>
