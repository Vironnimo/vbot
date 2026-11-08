<script>
  import { untrack } from 'svelte';
  import { scheduleAutosave } from '../../../../webui/src/lib/autosave.js';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import SaveStatus from '../../../../webui/src/components/ui/SaveStatus.svelte';
  import TabList from '../../../../webui/src/components/ui/TabList.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import WorkflowDiagram from './WorkflowDiagram.svelte';
  import WorkflowStep from './WorkflowStep.svelte';
  import LimitsEditor from './LimitsEditor.svelte';
  import LibraryInsert from './LibraryInsert.svelte';
  import PublishTemplate from './PublishTemplate.svelte';
  import LinkedTemplates from './LinkedTemplates.svelte';
  import { createRecordEditor } from './recordEditor.svelte.js';
  import { failureText } from './presentation.js';
  import {
    MAX_STEPS,
    MAX_WORKFLOWS,
    SETUP_FIELDS,
    canonicalWorkflows,
    duplicateWorkflow,
    editableWorkflows,
    moveStep,
    newStep,
    newWorkflow,
    removeStep,
    sameExceptNameSpacing,
    setupDefaults,
    stepKey,
    workflowProblem,
  } from './workflows.js';

  let { project, model, setup = null } = $props();
  let deleteOpen = $state(false);
  let removing = $state(null);
  let selectedId = $state('');
  let selectedStepId = $state('');
  let libraryAction = $state('');
  let libraryError = $state('');
  // Effective Models the server resolved for Agent choices not yet saved,
  // keyed by the step's choice (`agentChoice`).
  let previewed = $state({});
  let previewError = $state('');
  const projectId = untrack(() => project.id);
  const defaults = setupDefaults(
    projectId,
    untrack(() => model.templates),
  );
  const projectFields = (record) => ({
    ...Object.fromEntries(
      SETUP_FIELDS.map((field) => [field, record[field] ?? defaults[field]]),
    ),
    project_id: projectId,
    workflows: editableWorkflows(record.workflows ?? defaults.workflows),
  });
  const normalize = (draft) => ({
    project_id: projectId,
    enabled: Boolean(draft.enabled),
    workflows: canonicalWorkflows(draft.workflows),
    default_workflow: draft.default_workflow,
  });
  const editor = createRecordEditor({
    identity: projectId,
    client: untrack(() => model.client),
    get record() {
      return setup;
    },
    defaults,
    project: projectFields,
    normalize,
    validate(draft) {
      const problem = workflowProblem(draft.workflows);
      if (problem) return problem;
      return '';
    },
    save: async (values, revision) => {
      const result = await model.call('setup.update', {
        ...values,
        expected_revision: revision,
      });
      model.acceptSavedSetup(result.setup, result.effective_models);
      return sameExceptNameSpacing(values.workflows, result.setup.workflows)
        ? { ...result.setup, workflows: values.workflows }
        : result.setup;
    },
    read: async () => {
      const result = await model.call('setup.get', { project_id: projectId });
      model.acceptSavedSetup(result.setup, result.effective_models);
      return result.setup;
    },
    onSave: () => {},
  });

  const workflows = $derived(editor.draft.workflows);
  const selected = $derived(
    workflows.find((workflow) => workflow.id === selectedId) ??
      workflows.find(
        (workflow) => workflow.id === editor.draft.default_workflow,
      ) ??
      workflows[0],
  );
  const selectedIndex = $derived.by(() => {
    const index = selected?.steps.findIndex(
      (step) => step.id === selectedStepId,
    );
    return index >= 0 ? index : 0;
  });
  const tabs = $derived(
    workflows.map((workflow) => ({
      id: workflow.id,
      label:
        workflow.id === editor.draft.default_workflow
          ? t('factory.defaultWorkflowTab', {
              name: workflow.name.trim() || t('factory.unnamedWorkflow'),
            })
          : workflow.name.trim() || t('factory.unnamedWorkflow'),
      panelId: 'factory-workflow-panel',
    })),
  );
  const addOptions = $derived([
    {
      value: 'blank',
      label: t('factory.blankWorkflow'),
      secondaryLabel: t('factory.blankWorkflowHelp'),
    },
    ...model.templates.map((template) => ({
      value: `template:${template.id}`,
      label: template.name,
      secondaryLabel: template.description,
      group: t('factory.templates'),
    })),
    ...model.library
      .filter((item) => !item.archived)
      .map((item) => ({
        value: `library:${item.id}`,
        label: item.name,
        secondaryLabel: t('factory.library.version', {
          version: item.latest_version ?? item.version,
        }),
        group: t('factory.library'),
      })),
  ]);
  async function addWorkflow(choice) {
    if (workflows.length >= MAX_WORKFLOWS) return;
    if (choice.startsWith('library:')) {
      try {
        const item = model.library.find(
          (entry) => entry.id === choice.slice('library:'.length),
        );
        const result = await model.call('library.insert', {
          id: item.id,
          version: item.latest_version ?? item.version,
        });
        if (workflows.length >= MAX_WORKFLOWS) return;
        const workflow = newWorkflow(workflows, result.workflow);
        editor.draft.workflows.push(workflow);
        selectedId = workflow.id;
        selectedStepId = '';
        libraryError = '';
      } catch (cause) {
        libraryError = failureText(cause);
      }
      return;
    }
    const template = choice.startsWith('template:')
      ? model.templates.find(
          (item) => item.id === choice.slice('template:'.length),
        )
      : null;
    const workflow = newWorkflow(workflows, template);
    editor.draft.workflows.push(workflow);
    selectedId = workflow.id;
    selectedStepId = '';
  }

  function replaceWorkflow(next, previous) {
    const index = workflows.findIndex((entry) => entry.id === previous.id);
    if (index < 0 || !sameExceptNameSpacing([workflows[index]], [previous]))
      return false;
    editor.draft.workflows[index] = editableWorkflows([next])[0];
    libraryError = '';
    return true;
  }

  function duplicate(workflow) {
    if (workflows.length >= MAX_WORKFLOWS) return;
    const copied = duplicateWorkflow(workflows, workflow);
    editor.draft.workflows.push(copied);
    selectedId = copied.id;
    selectedStepId = '';
  }

  function removeWorkflow() {
    const index = workflows.findIndex((item) => item.id === removing?.id);
    removing = null;
    if (index < 0 || workflows.length < 2) return;
    const [removed] = editor.draft.workflows.splice(index, 1);
    if (editor.draft.default_workflow === removed.id)
      editor.draft.default_workflow = editor.draft.workflows[0].id;
    selectedId = editor.draft.workflows[Math.max(0, index - 1)].id;
  }

  function addStep() {
    if (selected.steps.length >= MAX_STEPS) return;
    const step = newStep(selected.steps);
    selected.steps.push(step);
    selectedStepId = step.id;
  }

  function move(index, delta) {
    selectedStepId = selected.steps[index].id;
    selected.steps = moveStep($state.snapshot(selected.steps), index, delta);
  }

  function remove(index) {
    selected.steps = removeStep($state.snapshot(selected.steps), index);
    selectedStepId = selected.steps[Math.max(0, index - 1)]?.id ?? '';
  }

  // Effective Models: a chosen Model is its own; an Agent's Model comes from
  // the last save while the step's choice is unchanged, else from a
  // read-only preview.
  const agentChoice = (step) =>
    JSON.stringify([step.source, step.agent, step.model, step.effort]);

  function savedModel(workflow, step) {
    const saved = model.selectedSetup?.workflows
      ?.find((item) => item.id === workflow.id)
      ?.steps?.find((item) => item.id === step.id);
    const key = stepKey(workflow, step);
    return saved &&
      agentChoice(saved) === agentChoice(step) &&
      Object.hasOwn(model.setupEffectiveModels, key)
      ? model.setupEffectiveModels[key]
      : undefined;
  }

  function resolveModel(workflow, step) {
    if (!step?.agent) return undefined;
    if (step.model) return step.model;
    return savedModel(workflow, step) ?? previewed[agentChoice(step)];
  }

  function agentLabel(step) {
    if (!step.agent) return '';
    const found =
      step.source === 'team'
        ? project.agent_profiles?.find((item) => item.id === step.agent)
        : model.catalog.agents?.find((item) => item.id === step.agent);
    return found?.name ?? step.agent;
  }

  const unresolved = $derived.by(() => {
    const choices = {};
    for (const workflow of workflows)
      for (const step of workflow.steps)
        if (
          step.agent &&
          !step.model &&
          savedModel(workflow, step) === undefined &&
          !Object.hasOwn(previewed, agentChoice(step))
        )
          choices[agentChoice(step)] = {
            source: step.source,
            agent: step.agent,
            model: '',
            effort: step.effort,
          };
    return Object.entries(choices);
  });
  const unresolvedKey = $derived(
    JSON.stringify(unresolved.map(([choice]) => choice)),
  );

  // A preview resolves only the open choices, each in a step of its own, so
  // an unfinished draft elsewhere does not keep it from answering.
  $effect(() => {
    void unresolvedKey;
    const open = untrack(() => unresolved.slice(0, MAX_WORKFLOWS * MAX_STEPS));
    if (!open.length) return;
    const previews = [];
    for (let start = 0; start < open.length; start += MAX_STEPS)
      previews.push({
        id: `preview${previews.length}`,
        name: `Preview ${previews.length + 1}`,
        steps: open
          .slice(start, start + MAX_STEPS)
          .map(([, choice], position) => ({
            ...choice,
            id: `p${position}`,
            name: `Preview ${position + 1}`,
            task: 'Preview',
            after: [],
          })),
      });
    let cancelled = false;
    const cancel = scheduleAutosave(async () => {
      try {
        const result = await model.call('setup.preview', {
          project_id: projectId,
          workflows: previews,
        });
        if (cancelled) return;
        const found = result.effective_models ?? {};
        const next = { ...previewed };
        open.forEach(([choice], position) => {
          const chunk = Math.floor(position / MAX_STEPS);
          const key = `preview${chunk}/p${position % MAX_STEPS}`;
          if (Object.hasOwn(found, key)) next[choice] = found[key];
        });
        previewed = next;
        previewError = '';
      } catch (cause) {
        if (!cancelled) previewError = failureText(cause);
      }
    });
    return () => {
      cancelled = true;
      cancel();
    };
  });

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

  <section class="factory-section" aria-labelledby="factory-workflows-heading">
    <div class="factory-workflows-header">
      <h3 id="factory-workflows-heading">{t('factory.workflows')}</h3>
      <div class="factory-add-workflow">
        <Dropdown
          id="factory-add-workflow"
          ariaLabel={t('factory.addWorkflow')}
          value=""
          options={addOptions}
          placeholder={t('factory.addWorkflow')}
          disabled={workflows.length >= MAX_WORKFLOWS}
          onOpenChange={model.pickerOpen}
          onValueChange={addWorkflow}
        />
      </div>
    </div>
    <p class="factory-help">{t('factory.workflowsHelp')}</p>
    {#if workflows.length}
      <TabList
        class="factory-workflow-tabs"
        ariaLabel={t('factory.workflows')}
        idPrefix="factory-workflow"
        items={tabs}
        value={selected?.id ?? ''}
        onChange={(id) => (selectedId = id)}
      />
    {/if}
    {#if selected}
      <div
        id="factory-workflow-panel"
        class="factory-workflow"
        role="tabpanel"
        data-workflow-id={selected.id}
      >
        <div class="factory-fields">
          <div class="factory-field">
            <label for="factory-workflow-name"
              >{t('factory.workflowName')}</label
            ><TextField
              id="factory-workflow-name"
              maxlength="80"
              value={selected.name}
              onInput={(value) => (selected.name = value)}
            />
          </div>
          <div class="factory-workflow-actions">
            {#if selected.id === editor.draft.default_workflow}<span
                class="factory-default-badge"
                >{t('factory.defaultWorkflow')}</span
              >{:else}<Button
                variant="tertiary"
                onClick={() => (editor.draft.default_workflow = selected.id)}
                >{t('factory.makeDefault')}</Button
              >{/if}
            <Button
              variant="tertiary"
              disabled={workflows.length >= MAX_WORKFLOWS}
              onClick={() => duplicate(selected)}
              >{t('factory.duplicateWorkflow')}</Button
            >
            <Button
              variant="tertiary"
              disabled={workflows.length < 2}
              onClick={() =>
                (removing = { id: selected.id, name: selected.name })}
              >{t('factory.deleteWorkflow')}</Button
            >
          </div>
          <div class="factory-field factory-field--wide">
            <label for="factory-workflow-description"
              >{t('factory.workflowDescription')}</label
            ><TextArea
              id="factory-workflow-description"
              maxlength="2000"
              rows={2}
              autoGrow
              value={selected.description}
              onInput={(value) => (selected.description = value)}
            />
          </div>
        </div>
        <LimitsEditor
          bind:value={selected.limits}
          prefix="factory-workflow-limit"
          label={t('factory.limit.card')}
        />
        <p class="factory-help">{t('factory.limit.help')}</p>
        <LinkedTemplates
          {model}
          workflow={selected}
          onReplace={replaceWorkflow}
        />
        <h4 class="factory-steps-heading">{t('factory.steps')}</h4>
        <p class="factory-help">{t('factory.stepsHelp')}</p>
        <WorkflowDiagram
          workflow={selected}
          {agentLabel}
          selectedId={selected.steps[selectedIndex]?.id ?? ''}
          onSelect={(id) => (selectedStepId = id)}
        />
        <div class="factory-actions">
          <Button
            variant="secondary"
            disabled={selected.steps.length >= MAX_STEPS}
            onClick={addStep}>{t('factory.addStep')}</Button
          >
          <Button
            variant="secondary"
            disabled={!model.library.some((item) => !item.archived) ||
              selected.steps.length >= MAX_STEPS}
            onClick={() => (libraryAction = 'insert')}
            >{t('factory.library.insert')}</Button
          >
          <Button variant="tertiary" onClick={() => (libraryAction = 'publish')}
            >{t('factory.library.saveTemplate')}</Button
          >
        </div>
        {#if selected.steps[selectedIndex]}
          {#key `${selected.id}:${selected.steps[selectedIndex].id}`}
            <WorkflowStep
              workflow={selected}
              bind:step={selected.steps[selectedIndex]}
              index={selectedIndex}
              {project}
              {model}
              resolve={(step) => resolveModel(selected, step)}
              onMove={(delta) => move(selectedIndex, delta)}
              onRemove={() => remove(selectedIndex)}
            />
          {/key}
        {/if}
      </div>
    {/if}
  </section>
  {#if previewError}<Banner variant="error" role="alert">{previewError}</Banner
    >{/if}
  {#if libraryError}<Banner variant="error" role="alert">{libraryError}</Banner
    >{/if}

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

{#if libraryAction === 'insert'}<LibraryInsert
    {model}
    workflow={selected}
    onInsert={replaceWorkflow}
    onClose={() => (libraryAction = '')}
  />{/if}
{#if libraryAction === 'publish'}<PublishTemplate
    {model}
    workflow={selected}
    onLinked={replaceWorkflow}
    onClose={() => (libraryAction = '')}
  />{/if}

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
{#if removing}
  <ConfirmDialog
    title={t('factory.deleteWorkflowTitle', {
      name: removing.name.trim() || t('factory.unnamedWorkflow'),
    })}
    body={t('factory.deleteWorkflowHelp')}
    confirmLabel={t('factory.deleteWorkflow')}
    onConfirm={removeWorkflow}
    onCancel={() => (removing = null)}
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
  .factory-workflows-header {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    justify-content: space-between;
  }
  .factory-workflows-header h3 {
    margin: 0;
  }
  .factory-add-workflow {
    min-width: 200px;
  }
  .factory-setup-editor :global(.factory-workflow-tabs) {
    margin: 18px 0 20px;
    overflow-x: auto;
  }
  .factory-workflow-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: end;
    justify-content: end;
    gap: 4px;
  }
  .factory-default-badge {
    align-self: center;
    border: 1px solid var(--accent);
    color: var(--accent);
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
    margin-right: 8px;
  }
  .factory-steps-heading {
    margin: 24px 0 6px;
    font-size: 13px;
  }
  .factory-workflow > :global(.factory-actions) {
    margin: 12px 0 16px;
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
  .factory-setup-editor > :global(.banner) {
    margin-bottom: 18px;
  }
</style>
