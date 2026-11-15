<script>
  import { untrack } from 'svelte';
  import { scheduleAutosave } from '@vbot/extension-sdk/lib/autosave.js';
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Banner from '@vbot/extension-sdk/ui/Banner.svelte';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Checkbox from '@vbot/extension-sdk/ui/Checkbox.svelte';
  import ConfirmDialog from '@vbot/extension-sdk/ui/ConfirmDialog.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import SaveStatus from '@vbot/extension-sdk/ui/SaveStatus.svelte';
  import TextArea from '@vbot/extension-sdk/ui/TextArea.svelte';
  import TextField from '@vbot/extension-sdk/ui/TextField.svelte';
  import Toggle from '@vbot/extension-sdk/ui/Toggle.svelte';
  import LimitsEditor from './LimitsEditor.svelte';
  import TriggerEditor from './TriggerEditor.svelte';
  import WorkflowDiagram from './WorkflowDiagram.svelte';
  import WorkflowStep from './WorkflowStep.svelte';
  import { createRecordEditor } from './recordEditor.svelte.js';
  import { failureText } from './presentation.js';
  import {
    MAX_STEPS,
    MAX_TRIGGERS,
    canonicalWorkflow,
    duplicateWorkflow,
    editableWorkflow,
    moveStep,
    newStep,
    newTrigger,
    newWorkflow,
    removeStep,
    sameExceptNameSpacing,
    stepKey,
    workflowProblem,
  } from './workflows.js';

  // One Workflow: its steps, triggers and limits. A saved Workflow saves
  // itself as the user edits; a new one, `workflow` null, starts from the
  // template the user picked and is created with an explicit button.
  let { model, onNavigate, workflow = null } = $props();
  let deleteOpen = $state(false);
  let selectedStepId = $state('');
  // Effective Models the server resolved for Agent choices not yet saved,
  // keyed by the step's choice (`agentChoice`).
  let previewed = $state({});
  let previewError = $state('');

  const defaults = untrack(() => {
    const choice = model.newTemplate;
    if (choice.startsWith('copy:')) {
      const source = model.workflows.find(
        (item) => item.id === choice.slice('copy:'.length),
      );
      if (source) return duplicateWorkflow(model.workflows, source);
    }
    return newWorkflow(
      model.workflows,
      model.templates.find((item) => item.id === choice) ?? null,
    );
  });
  const editor = createRecordEditor({
    identity: untrack(() => workflow?.id ?? 'new'),
    client: untrack(() => model.client),
    get record() {
      return workflow;
    },
    defaults,
    project: editableWorkflow,
    normalize: canonicalWorkflow,
    validate: (draft) => workflowProblem(draft, model.workflows),
    save: async (values, revision) => {
      const result = await model.call('workflow.save', {
        workflow: values,
        ...(revision ? { expected_revision: revision } : {}),
      });
      model.acceptSavedWorkflow(result.workflow, result.effective_models);
      return sameExceptNameSpacing(values, result.workflow)
        ? { ...result.workflow, ...values, id: result.workflow.id }
        : result.workflow;
    },
    read: async () => {
      const result = await model.call('workflow.get', { id: workflow.id });
      model.acceptSavedWorkflow(result.workflow, result.effective_models);
      return result.workflow;
    },
    onSave: (record) => {
      if (!workflow)
        void model.client.replaceRoute(
          `/workflows/${encodeURIComponent(record.id)}`,
        );
    },
  });

  const draft = $derived(editor.draft);
  const project = $derived(
    (model.catalog.projects ?? []).find(
      (item) => item.id === draft.project_id,
    ) ?? null,
  );
  // The saved Workflow with the host's trigger status.
  const saved = $derived(
    workflow
      ? (model.workflows.find((item) => item.id === workflow.id) ?? workflow)
      : null,
  );
  const selectedIndex = $derived.by(() => {
    const index = draft.steps.findIndex((step) => step.id === selectedStepId);
    return index >= 0 ? index : 0;
  });
  const projectOptions = $derived([
    {
      value: '',
      label: t('factory.anyProject'),
      secondaryLabel: t('factory.anyProjectHelp'),
    },
    ...(model.catalog.projects ?? []).map((item) => ({
      value: item.id,
      label: item.name,
    })),
    ...(draft.project_id &&
    !(model.catalog.projects ?? []).some((item) => item.id === draft.project_id)
      ? [
          {
            value: draft.project_id,
            label: draft.project_id,
            secondaryLabel: t('factory.projectUnavailable'),
            disabled: true,
          },
        ]
      : []),
  ]);

  function addStep() {
    if (draft.steps.length >= MAX_STEPS) return;
    const step = newStep(draft.steps);
    draft.steps.push(step);
    selectedStepId = step.id;
  }

  function move(index, delta) {
    selectedStepId = draft.steps[index].id;
    draft.steps = moveStep($state.snapshot(draft.steps), index, delta);
  }

  function remove(index) {
    draft.steps = removeStep($state.snapshot(draft.steps), index);
    selectedStepId = draft.steps[Math.max(0, index - 1)]?.id ?? '';
  }

  function addTrigger(type) {
    if (draft.triggers.length >= MAX_TRIGGERS) return;
    draft.triggers.push(newTrigger(draft.triggers, type));
  }

  // Effective Models: a chosen Model is its own; an Agent's Model comes from
  // the last save while the step's choice is unchanged, else from a
  // read-only preview.
  const agentChoice = (step) =>
    JSON.stringify([
      step.source,
      step.agent,
      step.model,
      step.effort,
      step.source === 'team' ? draft.project_id : '',
    ]);

  function savedModel(step) {
    const stored = model.selectedWorkflow;
    if (!workflow || stored?.id !== workflow.id) return undefined;
    const before = stored.steps?.find((item) => item.id === step.id);
    const key = stepKey(stored, step);
    return before &&
      agentChoice(before) === agentChoice(step) &&
      stored.project_id === draft.project_id &&
      Object.hasOwn(model.workflowEffectiveModels, key)
      ? model.workflowEffectiveModels[key]
      : undefined;
  }

  function resolveModel(step) {
    if (step?.kind !== 'agent' || !step.agent) return undefined;
    if (step.model) return step.model;
    return savedModel(step) ?? previewed[agentChoice(step)];
  }

  function agentLabel(step) {
    if (step.kind === 'workflow')
      return (
        model.workflows.find((item) => item.id === step.workflow)?.name ??
        step.workflow
      );
    if (!step.agent) return '';
    const found =
      step.source === 'team'
        ? project?.agent_profiles?.find((item) => item.id === step.agent)
        : model.catalog.agents?.find((item) => item.id === step.agent);
    return found?.name ?? step.agent;
  }

  const unresolved = $derived.by(() => {
    const choices = {};
    for (const step of draft.steps)
      if (
        step.kind === 'agent' &&
        step.agent &&
        !step.model &&
        (step.source !== 'team' || draft.project_id) &&
        savedModel(step) === undefined &&
        !Object.hasOwn(previewed, agentChoice(step))
      )
        choices[agentChoice(step)] = {
          kind: 'agent',
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
  // an unfinished draft does not keep it from answering.
  $effect(() => {
    void unresolvedKey;
    const open = untrack(() => unresolved);
    const projectId = untrack(() => draft.project_id);
    if (!open.length) return;
    let cancelled = false;
    const cancel = scheduleAutosave(async () => {
      try {
        const next = { ...previewed };
        for (let start = 0; start < open.length; start += MAX_STEPS) {
          const chunk = open.slice(start, start + MAX_STEPS);
          const result = await model.call('workflow.preview', {
            workflow: {
              id: 'preview0',
              name: 'Preview',
              enabled: true,
              project_id: projectId,
              final_approval: false,
              steps: chunk.map(([, choice], position) => ({
                ...choice,
                id: `p${position}`,
                name: `Preview ${position + 1}`,
                task: 'Preview',
                after: [],
                approval: false,
                returns_to: '',
                rounds: 2,
              })),
              triggers: [],
            },
          });
          if (cancelled) return;
          const found = result.effective_models ?? {};
          chunk.forEach(([choice], position) => {
            const key = `preview0/p${position}`;
            if (Object.hasOwn(found, key)) next[choice] = found[key];
          });
        }
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

  async function deleteWorkflow() {
    if (!(await editor.flush())) return;
    if (await model.mutate('workflow.delete', { id: workflow.id }))
      deleteOpen = false;
  }
</script>

<article class="factory-workflow-editor" data-workflow-id={draft.id}>
  <header class="factory-editor-header">
    <div class="factory-enable-row">
      <h2>{draft.name.trim() || t('factory.unnamedWorkflow')}</h2>
      <span class="factory-enable-toggle"
        ><span id="factory-workflow-enabled-label"
          >{draft.enabled ? t('factory.enabled') : t('factory.paused')}</span
        ><Toggle
          checked={Boolean(draft.enabled)}
          ariaLabel={t('factory.enableWorkflow')}
          onChange={(value) => (draft.enabled = value)}
        /></span
      >
    </div>
    <p class="factory-help">{t('factory.enableWorkflowHelp')}</p>
  </header>

  <section class="factory-section">
    <div class="factory-fields">
      <div class="factory-field">
        <label for="factory-workflow-name">{t('factory.workflowName')}</label
        ><TextField
          id="factory-workflow-name"
          maxlength="80"
          value={draft.name}
          onInput={(value) => (draft.name = value)}
        />
      </div>
      <div class="factory-field">
        <label
          id="factory-workflow-project-label"
          for="factory-workflow-project">{t('factory.project')}</label
        >
        <Dropdown
          id="factory-workflow-project"
          ariaLabelledby="factory-workflow-project-label"
          value={draft.project_id}
          options={projectOptions}
          onOpenChange={model.pickerOpen}
          onValueChange={(value) => (draft.project_id = value)}
        />
      </div>
      <div class="factory-field factory-field--wide">
        <label for="factory-workflow-description"
          >{t('factory.workflowDescription')}</label
        ><TextArea
          id="factory-workflow-description"
          maxlength="2000"
          rows={2}
          autoGrow
          value={draft.description}
          onInput={(value) => (draft.description = value)}
        />
      </div>
    </div>
    <p class="factory-help">{t('factory.workflowProjectHelp')}</p>
  </section>

  <section class="factory-section" aria-labelledby="factory-steps-heading">
    <h3 id="factory-steps-heading">{t('factory.steps')}</h3>
    <p class="factory-help">{t('factory.stepsHelp')}</p>
    <WorkflowDiagram
      workflow={draft}
      {agentLabel}
      selectedId={draft.steps[selectedIndex]?.id ?? ''}
      onSelect={(id) => (selectedStepId = id)}
    />
    <div class="factory-actions">
      <Button
        variant="secondary"
        disabled={draft.steps.length >= MAX_STEPS}
        onClick={addStep}>{t('factory.addStep')}</Button
      >
    </div>
    {#if draft.steps[selectedIndex]}
      {#key draft.steps[selectedIndex].id}
        <WorkflowStep
          workflow={draft}
          bind:step={draft.steps[selectedIndex]}
          index={selectedIndex}
          {project}
          workflows={model.workflows}
          {model}
          resolve={resolveModel}
          onMove={(delta) => move(selectedIndex, delta)}
          onRemove={() => remove(selectedIndex)}
        />
      {/key}
    {/if}
    <div class="factory-final">
      <Checkbox
        id="factory-final-approval"
        checked={draft.final_approval}
        onChange={(value) => (draft.final_approval = value)}
        >{t('factory.finalApproval')}</Checkbox
      >
      <p class="factory-help">{t('factory.finalApprovalHelp')}</p>
    </div>
  </section>

  <section class="factory-section" aria-labelledby="factory-triggers-heading">
    <h3 id="factory-triggers-heading">{t('factory.triggers')}</h3>
    <p class="factory-help">{t('factory.triggersHelp')}</p>
    {#if !model.triggersAvailable}<Banner variant="warn" role="status"
        >{t('factory.triggersUnavailable')}</Banner
      >{/if}
    {#each draft.triggers as trigger, index (trigger.id)}
      <TriggerEditor
        bind:trigger={draft.triggers[index]}
        workflow={draft}
        saved={saved?.triggers?.find((item) => item.id === trigger.id) ?? null}
        {model}
        onRemove={() => draft.triggers.splice(index, 1)}
      />
    {/each}
    <div class="factory-actions">
      <Button
        variant="secondary"
        disabled={draft.triggers.length >= MAX_TRIGGERS}
        onClick={() => addTrigger('schedule')}
        >{t('factory.addSchedule')}</Button
      >
      <Button
        variant="secondary"
        disabled={draft.triggers.length >= MAX_TRIGGERS}
        onClick={() => addTrigger('webhook')}>{t('factory.addWebhook')}</Button
      >
    </div>
  </section>

  <section class="factory-section">
    <LimitsEditor
      bind:value={draft.limits}
      prefix="factory-workflow-limit"
      label={t('factory.limit.card')}
    />
    <p class="factory-help">{t('factory.limit.help')}</p>
  </section>

  {#if previewError}<Banner variant="error" role="alert">{previewError}</Banner
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
        onClick={() => (deleteOpen = true)}
        >{t('factory.deleteWorkflow')}</Button
      >{:else}<Button
        variant="tertiary"
        onClick={() => onNavigate('/workflows')}>{t('common.cancel')}</Button
      >{/if}
    <div class="factory-save-status">
      {#if editor.exists}<SaveStatus
          saving={editor.saving}
          pending={editor.hasChanges()}
          onClick={editor.save}
        />{:else}<Button
          variant="primary"
          loading={editor.saving}
          onClick={editor.save}>{t('factory.createWorkflow')}</Button
        >{/if}
    </div>
  </div>
</article>

{#if deleteOpen}
  <ConfirmDialog
    title={t('factory.deleteWorkflowTitle', {
      name: draft.name.trim() || t('factory.unnamedWorkflow'),
    })}
    body={t('factory.deleteWorkflowHelp')}
    confirmLabel={t('factory.deleteWorkflow')}
    busy={Boolean(model.pending)}
    error={model.error}
    onConfirm={deleteWorkflow}
    onCancel={() => (deleteOpen = false)}
  />
{/if}

<style>
  .factory-editor-header {
    margin-bottom: 24px;
  }
  .factory-editor-header h2 {
    margin: 0;
    font-size: 20px;
    overflow-wrap: anywhere;
  }
  .factory-enable-row {
    display: flex;
    gap: 18px;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }
  .factory-enable-toggle {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--muted);
  }
  .factory-section h3 {
    margin: 0 0 6px;
    font-size: 14px;
  }
  .factory-section > :global(.factory-actions) {
    margin: 12px 0 16px;
  }
  .factory-final {
    margin-top: 18px;
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
  .factory-workflow-editor > :global(.banner),
  .factory-section > :global(.banner) {
    margin-bottom: 18px;
  }
</style>
