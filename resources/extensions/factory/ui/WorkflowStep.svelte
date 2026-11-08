<script>
  import LimitsEditor from './LimitsEditor.svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Checkbox from '../../../../webui/src/components/ui/Checkbox.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import SearchableDropdown from '../../../../webui/src/components/SearchableDropdown.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
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
  import { repairSteps, returnTargets, stepName } from './workflows.js';

  // One step of the edited Workflow: who does it, what it does and when it
  // starts. It edits the draft object it gets; `resolve(step)` is the step's
  // effective Model: '' when its Agent has none, undefined while unknown.
  let {
    workflow,
    step = $bindable(),
    index,
    project,
    model,
    resolve,
    onMove,
    onRemove,
    requireAgent = true,
  } = $props();
  let showAllModels = $state(false);
  const prefix = $derived(`factory-${step.id}`);
  const count = $derived(workflow.steps.length);
  const earlier = $derived(workflow.steps.slice(0, index));
  const targets = $derived(returnTargets(workflow.steps, index));
  const agents = $derived(model.catalog.agents ?? []);
  const team = $derived(project.agent_profiles ?? []);
  const models = $derived(model.catalog.models ?? []);
  const name = $derived(stepName(step, index));
  const resolved = $derived(resolve(step));
  const targetOptions = $derived([
    { value: '', label: t('factory.returnsNowhere') },
    ...targets.map((item) => ({
      value: item.id,
      label: stepName(item, workflow.steps.indexOf(item)),
    })),
  ]);
  // Judging work with the Model that did it is less independent.
  const sameModel = $derived.by(() => {
    const target = workflow.steps.find((item) => item.id === step.returns_to);
    return Boolean(target && resolved && resolve(target) === resolved);
  });

  function toggleAfter(id, checked) {
    step.after = checked
      ? earlier
          .map((item) => item.id)
          .filter((item) => item === id || step.after.includes(item))
      : step.after.filter((item) => item !== id);
    repairSteps(workflow.steps);
  }

  // One picker over every Agent: the Identity Agents, then the Project's
  // Team. A value is `<source>:<id>`, split into the two fields.
  const agentValue = $derived(step.agent ? `${step.source}:${step.agent}` : '');
  const agentOptions = $derived.by(() => {
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
    if (agentValue && !options.some((option) => option.value === agentValue))
      options.push({
        value: agentValue,
        label: step.agent,
        secondaryLabel: t('factory.agentUnavailable'),
        disabled: true,
      });
    return options;
  });

  function selectAgent(value) {
    const separator = value.indexOf(':');
    step.source = separator > 0 ? value.slice(0, separator) : step.source;
    step.agent = separator > 0 ? value.slice(separator + 1) : '';
  }

  const allModels = $derived(
    buildModelSelectOptions({
      models,
      modelOnly: true,
      selectedModelValue: step.model,
      emptyLabel: t('factory.inheritModel'),
    }),
  );
  const visibleModels = $derived(
    filterModelSelectOptions(allModels, {
      showAll: showAllModels,
      selectedModelValue: step.model,
    }),
  );
  const effortOptions = $derived(
    effortOptionsForReasoning(
      reasoningForModelValue(resolved || '', models),
    ).map((value) => ({
      value,
      label:
        value === ''
          ? t('factory.defaultEffort')
          : t(`agents.form.thinkingEffortOption.${value}`),
    })),
  );

  function selectModel(value) {
    step.model = value;
    step.effort = effortForReasoning(
      step.effort,
      reasoningForModelValue(resolve(step) || '', models),
    );
  }
</script>

<article
  class="factory-step"
  data-step-id={step.id}
  aria-labelledby={`${prefix}-heading`}
>
  <header class="factory-step-header">
    <h4 id={`${prefix}-heading`}>
      <span class="factory-step-number">{index + 1}</span>{name}
    </h4>
    <div class="factory-step-tools">
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('factory.moveStepUp', { step: name })}
        disabled={index === 0}
        onClick={() => onMove(-1)}><span aria-hidden="true">↑</span></Button
      >
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('factory.moveStepDown', { step: name })}
        disabled={index === count - 1}
        onClick={() => onMove(1)}><span aria-hidden="true">↓</span></Button
      >
      <Button
        variant="tertiary"
        ariaLabel={t('factory.removeStep', { step: name })}
        disabled={count === 1}
        onClick={onRemove}>{t('factory.remove')}</Button
      >
    </div>
  </header>

  <div class="factory-fields">
    <div class="factory-field factory-field--wide">
      <label for={`${prefix}-name`}>{t('factory.stepName')}</label><TextField
        id={`${prefix}-name`}
        maxlength="80"
        value={step.name}
        onInput={(value) => (step.name = value)}
      />
    </div>
  </div>

  <div class="factory-step-agent">
    <div class="factory-field">
      <label id={`${prefix}-agent-label`} for={`${prefix}-agent`}
        >{t('factory.agent')}</label
      >
      <Dropdown
        id={`${prefix}-agent`}
        ariaLabelledby={`${prefix}-agent-label`}
        value={agentValue}
        options={agentOptions}
        invalid={requireAgent && !step.agent}
        onOpenChange={model.pickerOpen}
        onValueChange={selectAgent}
      />
    </div>
    <div class="factory-field">
      <label id={`${prefix}-model-label`} for={`${prefix}-model`}
        >{t('factory.model')}</label
      >
      <SearchableDropdown
        id={`${prefix}-model`}
        ariaLabelledby={`${prefix}-model-label`}
        value={step.model}
        options={visibleModels}
        placeholder={t('factory.inheritModel')}
        searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
        emptyLabel={t('agents.form.modelSearchEmpty')}
        footerActionLabel={modelFilterFooterLabel({
          showAll: showAllModels,
          hiddenCount: allModels.length - visibleModels.length,
        })}
        onFooterAction={() => (showAllModels = !showAllModels)}
        onOpenChange={model.pickerOpen}
        onValueChange={selectModel}
      />
    </div>
    <div class="factory-field">
      <label id={`${prefix}-effort-label`} for={`${prefix}-effort`}
        >{t('factory.effort')}</label
      >
      <Dropdown
        id={`${prefix}-effort`}
        ariaLabelledby={`${prefix}-effort-label`}
        value={step.effort}
        options={effortOptions}
        onValueChange={(value) => (step.effort = value)}
      />
    </div>
  </div>
  {#if step.agent && resolved === ''}<p
      class="factory-field-error"
      role="status"
    >
      {t('factory.modelUnresolved')}
    </p>{:else if step.agent && !step.model && resolved}<p
      class="factory-help"
      data-effective-model
    >
      {t('factory.effectiveModel', { model: resolved })}
    </p>{/if}

  <div class="factory-field factory-step-task">
    <label for={`${prefix}-task`}>{t('factory.stepTask')}</label><TextArea
      id={`${prefix}-task`}
      maxlength="20000"
      rows={4}
      autoGrow
      invalid={!step.task.trim()}
      placeholder={t('factory.stepTaskPlaceholder')}
      value={step.task}
      onInput={(value) => (step.task = value)}
    />
  </div>

  <fieldset class="factory-step-start">
    <legend>{t('factory.stepStarts')}</legend>
    {#if earlier.length}
      <p class="factory-help">{t('factory.stepAfterHelp')}</p>
      <div class="factory-step-after">
        {#each earlier as item, position (item.id)}
          <Checkbox
            id={`${prefix}-after-${item.id}`}
            checked={step.after.includes(item.id)}
            onChange={(checked) => toggleAfter(item.id, checked)}
            >{stepName(item, position)}</Checkbox
          >
        {/each}
      </div>
    {:else}
      <p class="factory-help">{t('factory.stepFirst')}</p>
    {/if}
    <Checkbox
      id={`${prefix}-approval`}
      checked={Boolean(step.approval)}
      onChange={(value) => (step.approval = value)}
      >{t('factory.stepApproval')}</Checkbox
    >
  </fieldset>

  {#if index > 0}
    <div class="factory-fields">
      <div class="factory-field">
        <label id={`${prefix}-returns-label`} for={`${prefix}-returns`}
          >{t('factory.stepReturnsTo')}</label
        ><Dropdown
          id={`${prefix}-returns`}
          ariaLabelledby={`${prefix}-returns-label`}
          value={step.returns_to}
          options={targetOptions}
          disabled={!targets.length}
          onValueChange={(value) => (step.returns_to = value)}
        />
        <p class="factory-help">
          {targets.length
            ? t('factory.stepReturnsHelp')
            : t('factory.stepReturnsNone')}
        </p>
      </div>
      {#if step.returns_to}
        <div class="factory-field">
          <label for={`${prefix}-rounds`}>{t('factory.stepRounds')}</label
          ><TextField
            id={`${prefix}-rounds`}
            type="number"
            min="1"
            max="20"
            step="1"
            aria-describedby={`${prefix}-rounds-help`}
            value={step.rounds}
            onInput={(value) => (step.rounds = value)}
          />
          <p id={`${prefix}-rounds-help`} class="factory-help">
            {t('factory.stepRoundsHelp')}
          </p>
        </div>
      {/if}
    </div>
  {/if}
  {#if sameModel}<p class="factory-step-warning" role="status">
      {t('factory.sameModel')}
    </p>{/if}
</article>

<LimitsEditor
  bind:value={step.limits}
  prefix={`${prefix}-limit`}
  label={t('factory.limit.step')}
/>
<p class="factory-help">{t('factory.limit.stepHelp')}</p>

<style>
  .factory-step {
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface-2);
    padding: 16px;
    min-width: 0;
    display: grid;
    gap: 14px;
  }
  .factory-step-header {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: center;
    justify-content: space-between;
  }
  .factory-step-header h4 {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-size: 13px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .factory-step-number {
    display: inline-grid;
    place-items: center;
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--surface-3);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
  }
  .factory-step-tools {
    display: flex;
    gap: 2px;
  }
  .factory-step-agent {
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(0, 1.2fr) minmax(0, 1fr);
    gap: 12px;
  }
  .factory-step-start {
    border: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 8px;
    min-width: 0;
  }
  .factory-step-start legend {
    padding: 0;
    margin-bottom: 6px;
    font-size: 12px;
    font-weight: 550;
  }
  .factory-step-start .factory-help {
    margin: 0;
  }
  .factory-step-after {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 18px;
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
  .factory-step-warning {
    margin: 0;
    border-left: 2px solid var(--amber);
    padding-left: 8px;
    font-size: 12px;
    line-height: 1.5;
  }
  @media (max-width: 720px) {
    .factory-step-agent {
      grid-template-columns: 1fr;
    }
  }
</style>
