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
      return !draft.worker_profile ||
        !draft.reviewer_profile ||
        !draft.base_branch.trim() ||
        draft.base_branch.trim().startsWith('-') ||
        !Number.isInteger(Number(draft.review_rounds)) ||
        Number(draft.review_rounds) < 1 ||
        Number(draft.review_rounds) > 20 ||
        !Number.isInteger(Number(draft.turn_budget)) ||
        Number(draft.turn_budget) < 1 ||
        Number(draft.turn_budget) > 1000
        ? t('factory.invalidSetup')
        : '';
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
  const roles = $derived([
    {
      id: 'worker',
      label: t('factory.worker'),
      profile: 'worker_profile',
      model: 'worker_model',
      effort: 'worker_effort',
      empty: t('factory.inheritModel'),
    },
    {
      id: 'reviewer',
      label: t('factory.reviewer', { round: '' }).trim(),
      profile: 'reviewer_profile',
      model: 'reviewer_model',
      effort: 'reviewer_effort',
      empty: t('factory.inheritModel'),
    },
  ]);
  const team = $derived(project.agent_profiles ?? []);
  const models = $derived(model.catalog.models ?? []);
  const previewChoices = $derived({
    project_id: projectId,
    worker_profile: editor.draft.worker_profile,
    worker_model: editor.draft.worker_model,
    worker_effort: editor.draft.worker_effort,
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
  function effectiveRoleModel(role) {
    const saved = model.selectedSetup;
    if (editor.draft[`${role}_model`]) return editor.draft[`${role}_model`];
    return saved &&
      editor.draft[`${role}_profile`] === saved[`${role}_profile`] &&
      editor.draft[`${role}_model`] === saved[`${role}_model`]
      ? model.setupEffectiveModels[role]
      : previewed?.key === previewKey
        ? previewed.models[role]
        : '';
  }
  const workerModel = $derived(effectiveRoleModel('worker'));
  const reviewerModel = $derived(effectiveRoleModel('reviewer'));
  const sameModel = $derived(
    Boolean(workerModel && reviewerModel && workerModel === reviewerModel),
  );
  const integrationOptions = $derived([
    { value: 'merged', label: t('factory.checkMerged') },
    { value: 'reference_only', label: t('factory.checkReference') },
  ]);

  function profileOptions(selected) {
    const options = [
      { value: '', label: t('factory.selectProfile') },
      ...team.map((profile) => ({
        value: profile.id,
        label: profile.name,
        secondaryLabel: profile.source,
      })),
    ];
    if (selected && !options.some((option) => option.value === selected))
      options.push({
        value: selected,
        label: selected,
        secondaryLabel: t('factory.profileUnavailable'),
        disabled: true,
      });
    return options;
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
    {#if !team.length}<Banner variant="warn">{t('factory.noProfiles')}</Banner
      >{/if}
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
        <div class="factory-field">
          <label
            id={`factory-label-${role.profile}`}
            for={`factory-${role.profile}`}>{t('factory.teamProfile')}</label
          >
          <Dropdown
            id={`factory-${role.profile}`}
            ariaLabelledby={`factory-label-${role.profile}`}
            value={editor.draft[role.profile]}
            options={profileOptions(editor.draft[role.profile])}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (editor.draft[role.profile] = value)}
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
  <section class="factory-section">
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
          value={editor.draft.turn_budget}
          onInput={(value) => (editor.draft.turn_budget = value)}
        />
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
  .factory-setup-editor > :global(.banner) {
    margin-bottom: 18px;
  }
</style>
