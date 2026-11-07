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
  import WorkflowStage from './WorkflowStage.svelte';
  import { createRecordEditor } from './recordEditor.svelte.js';
  import { failureText } from './presentation.js';
  import {
    MAX_STAGES,
    MAX_WORKFLOWS,
    SETUP_FIELDS,
    canonicalWorkflows,
    duplicateWorkflow,
    editableWorkflows,
    laneKey,
    newStage,
    newWorkflow,
    sameExceptNameSpacing,
    setupDefaults,
    turnBudgetValid,
    workflowProblem,
    workStages,
  } from './workflows.js';

  let { project, model, setup = null } = $props();
  let deleteOpen = $state(false);
  let removing = $state(null);
  let selectedId = $state('');
  // Effective Models the server resolved for lane choices not yet saved,
  // keyed by the lane's choice (`laneChoice`).
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
    turn_budget: Number(draft.turn_budget),
    base_branch: draft.base_branch.trim(),
    integration_check: draft.integration_check,
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
      if (!turnBudgetValid(draft.turn_budget))
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
  ]);
  const integrationOptions = $derived([
    { value: 'merged', label: t('factory.checkMerged') },
    { value: 'reference_only', label: t('factory.checkReference') },
  ]);

  function addWorkflow(choice) {
    if (workflows.length >= MAX_WORKFLOWS) return;
    const template = choice.startsWith('template:')
      ? model.templates.find(
          (item) => item.id === choice.slice('template:'.length),
        )
      : null;
    const workflow = newWorkflow(workflows, template);
    editor.draft.workflows.push(workflow);
    selectedId = workflow.id;
  }

  function duplicate(workflow) {
    if (workflows.length >= MAX_WORKFLOWS) return;
    const copied = duplicateWorkflow(workflows, workflow);
    editor.draft.workflows.push(copied);
    selectedId = copied.id;
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

  function addStage(kind) {
    if (selected.stages.length < MAX_STAGES)
      selected.stages.push(newStage(kind, selected.stages));
  }

  function moveStage(index, delta) {
    const stages = $state.snapshot(selected.stages);
    const [stage] = stages.splice(index, 1);
    stages.splice(index + delta, 0, stage);
    selected.stages = stages;
  }

  // Effective Models: a chosen Model is its own; an Agent's Model comes from
  // the last save while the lane is unchanged, else from a read-only preview.
  const laneChoice = (lane) =>
    JSON.stringify([lane.source, lane.agent, lane.model, lane.effort]);

  function savedLane(key) {
    const [workflowId, stageId, index] = key.split('/');
    return model.selectedSetup?.workflows
      ?.find((workflow) => workflow.id === workflowId)
      ?.stages?.find((stage) => stage.id === stageId)?.lanes?.[Number(index)];
  }

  function savedModel(workflow, stage, index) {
    const key = laneKey(workflow, stage, index);
    const saved = savedLane(key);
    return saved &&
      laneChoice(saved) === laneChoice(stage.lanes[index]) &&
      Object.hasOwn(model.setupEffectiveModels, key)
      ? model.setupEffectiveModels[key]
      : undefined;
  }

  function resolveModel(workflow, stage, index) {
    const lane = stage.lanes[index];
    if (!lane?.agent) return undefined;
    if (lane.model) return lane.model;
    return savedModel(workflow, stage, index) ?? previewed[laneChoice(lane)];
  }

  const unresolved = $derived.by(() => {
    const choices = {};
    for (const workflow of workflows)
      for (const stage of workflow.stages)
        stage.lanes.forEach((lane, index) => {
          if (
            lane.agent &&
            !lane.model &&
            savedModel(workflow, stage, index) === undefined &&
            !Object.hasOwn(previewed, laneChoice(lane))
          )
            choices[laneChoice(lane)] = {
              source: lane.source,
              agent: lane.agent,
              model: '',
              effort: lane.effort,
            };
        });
    return Object.entries(choices);
  });
  const unresolvedKey = $derived(
    JSON.stringify(unresolved.map(([choice]) => choice)),
  );

  // A preview resolves only the open choices, each in a lane of its own, so
  // an unfinished draft elsewhere does not keep it from answering.
  $effect(() => {
    void unresolvedKey;
    const open = untrack(() => unresolved.slice(0, 200));
    if (!open.length) return;
    const stages = [];
    for (let start = 0; start < open.length; start += 10)
      stages.push({
        id: `p${stages.length}`,
        name: `Preview ${stages.length + 1}`,
        kind: 'work',
        output: 'document',
        instructions: '',
        approval: false,
        lanes: open.slice(start, start + 10).map(([, lane]) => lane),
      });
    let cancelled = false;
    const cancel = scheduleAutosave(async () => {
      try {
        const result = await model.call('setup.preview', {
          project_id: projectId,
          workflows: [{ id: 'preview', name: 'Preview', stages }],
        });
        if (cancelled) return;
        const found = result.effective_models ?? {};
        const next = { ...previewed };
        open.forEach(([choice], position) => {
          const key = `preview/p${Math.floor(position / 10)}/${position % 10}`;
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
        <h4 class="factory-stages-heading">{t('factory.stages')}</h4>
        <ol class="factory-stage-list">
          {#each selected.stages as stage, index (stage.id)}<li>
              <WorkflowStage
                workflow={selected}
                bind:stage={selected.stages[index]}
                {index}
                {project}
                {model}
                resolve={(item, lane) => resolveModel(selected, item, lane)}
                onMove={(delta) => moveStage(index, delta)}
                onRemove={() => selected.stages.splice(index, 1)}
              />
            </li>{/each}
        </ol>
        <div class="factory-actions">
          <Button
            variant="secondary"
            disabled={selected.stages.length >= MAX_STAGES}
            onClick={() => addStage('work')}>{t('factory.addWorkStage')}</Button
          >
          <Button
            variant="secondary"
            disabled={selected.stages.length >= MAX_STAGES ||
              !workStages(selected.stages).length}
            onClick={() => addStage('review')}
            >{t('factory.addReviewStage')}</Button
          >
        </div>
      </div>
    {/if}
  </section>
  {#if previewError}<Banner variant="error" role="alert">{previewError}</Banner
    >{/if}

  <section class="factory-section" aria-labelledby="factory-setup-execution">
    <h3 id="factory-setup-execution">{t('factory.execution')}</h3>
    <div class="factory-fields">
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
  .factory-stages-heading {
    margin: 24px 0 12px;
    font-size: 13px;
  }
  .factory-stage-list {
    list-style: none;
    padding: 0;
    margin: 0 0 14px;
    display: grid;
    gap: 12px;
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
