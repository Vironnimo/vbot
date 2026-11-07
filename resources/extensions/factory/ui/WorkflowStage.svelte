<script>
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
  import { stageKindLabel } from './presentation.js';
  import {
    MAX_LANES,
    blankLane,
    integratorIndex,
    workStages,
  } from './workflows.js';

  // One stage of the edited Workflow. It edits the draft objects it gets;
  // `resolve(stage, lane)` is the lane's effective Model: '' when its Agent
  // has none, undefined while unknown.
  let {
    workflow,
    stage = $bindable(),
    index,
    project,
    model,
    resolve,
    onMove,
    onRemove,
  } = $props();
  let showAllModels = $state(false);
  const prefix = $derived(`factory-${stage.id}`);
  const count = $derived(workflow.stages.length);
  const earlier = $derived(workStages(workflow.stages, index));
  const integrates = $derived(integratorIndex(workflow.stages) === index);
  const agents = $derived(model.catalog.agents ?? []);
  const team = $derived(project.agent_profiles ?? []);
  const models = $derived(model.catalog.models ?? []);
  const stageName = $derived(
    stage.name.trim() || t('factory.stagePosition', { position: index + 1 }),
  );
  const kindOptions = $derived([
    { value: 'work', label: t('factory.kind.work') },
    { value: 'review', label: t('factory.kind.review') },
  ]);
  const outputOptions = $derived([
    {
      value: 'changes',
      label: t('factory.output.changes'),
      secondaryLabel: t('factory.output.changesHelp'),
    },
    {
      value: 'document',
      label: t('factory.output.document'),
      secondaryLabel: t('factory.output.documentHelp'),
    },
  ]);
  const targetOptions = $derived(
    earlier.map((item) => ({
      value: item.id,
      label: item.name.trim() || item.id,
    })),
  );
  // A review judged by the Model that did the work is less independent.
  const sameModel = $derived.by(() => {
    if (stage.kind !== 'review') return false;
    const target = workflow.stages.find((item) => item.id === stage.target);
    if (!target) return false;
    const worked = new Set(
      target.lanes.map((_, lane) => resolve(target, lane)).filter(Boolean),
    );
    return stage.lanes.some((_, lane) => worked.has(resolve(stage, lane)));
  });

  function selectKind(kind) {
    stage.kind = kind;
    if (kind === 'review' && !earlier.some((item) => item.id === stage.target))
      stage.target = earlier.at(-1)?.id ?? '';
  }

  // One picker per lane over every Agent: the Identity Agents, then the
  // Project's Team. A value is `<source>:<id>`, split into the two fields.
  const agentValue = (lane) =>
    lane.agent ? `${lane.source}:${lane.agent}` : '';

  function agentOptions(lane) {
    const selected = agentValue(lane);
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
        label: lane.agent,
        secondaryLabel: t('factory.agentUnavailable'),
        disabled: true,
      });
    return options;
  }

  function selectAgent(lane, value) {
    const separator = value.indexOf(':');
    lane.source = separator > 0 ? value.slice(0, separator) : lane.source;
    lane.agent = separator > 0 ? value.slice(separator + 1) : '';
  }

  const laneModel = (laneIndex) => resolve(stage, laneIndex) || '';

  function effortOptions(laneIndex) {
    return effortOptionsForReasoning(
      reasoningForModelValue(laneModel(laneIndex), models),
    ).map((value) => ({
      value,
      label:
        value === ''
          ? t('factory.defaultEffort')
          : t(`agents.form.thinkingEffortOption.${value}`),
    }));
  }

  function selectModel(lane, laneIndex, value) {
    lane.model = value;
    lane.effort = effortForReasoning(
      lane.effort,
      reasoningForModelValue(laneModel(laneIndex), models),
    );
  }

  function addLane() {
    if (stage.lanes.length < MAX_LANES) stage.lanes.push(blankLane());
  }
</script>

<article
  class="factory-stage"
  data-stage-id={stage.id}
  data-stage-kind={stage.kind}
  aria-labelledby={`${prefix}-heading`}
>
  <header class="factory-stage-header">
    <h4 id={`${prefix}-heading`}>
      <span class="factory-stage-number">{index + 1}</span>{stageName}<span
        class="factory-subtle">{stageKindLabel(stage)}</span
      >
    </h4>
    <div class="factory-stage-tools">
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('factory.moveStageUp', { stage: stageName })}
        disabled={index === 0}
        onClick={() => onMove(-1)}><span aria-hidden="true">↑</span></Button
      >
      <Button
        variant="tertiary"
        icon
        ariaLabel={t('factory.moveStageDown', { stage: stageName })}
        disabled={index === count - 1}
        onClick={() => onMove(1)}><span aria-hidden="true">↓</span></Button
      >
      <Button
        variant="tertiary"
        ariaLabel={t('factory.removeStage', { stage: stageName })}
        disabled={count === 1}
        onClick={onRemove}>{t('factory.remove')}</Button
      >
    </div>
  </header>
  <div class="factory-fields">
    <div class="factory-field">
      <label for={`${prefix}-name`}>{t('factory.stageName')}</label><TextField
        id={`${prefix}-name`}
        maxlength="80"
        value={stage.name}
        onInput={(value) => (stage.name = value)}
      />
    </div>
    <div class="factory-field">
      <label id={`${prefix}-kind-label`} for={`${prefix}-kind`}
        >{t('factory.stageKind')}</label
      ><Dropdown
        id={`${prefix}-kind`}
        ariaLabelledby={`${prefix}-kind-label`}
        value={stage.kind}
        options={kindOptions}
        disabled={index === 0 && stage.kind === 'work'}
        onValueChange={selectKind}
      />
    </div>
    {#if stage.kind === 'work'}
      <div class="factory-field">
        <label id={`${prefix}-output-label`} for={`${prefix}-output`}
          >{t('factory.stageOutput')}</label
        ><Dropdown
          id={`${prefix}-output`}
          ariaLabelledby={`${prefix}-output-label`}
          value={stage.output}
          options={outputOptions}
          onValueChange={(value) => (stage.output = value)}
        />
      </div>
    {:else}
      <div class="factory-field">
        <label id={`${prefix}-target-label`} for={`${prefix}-target`}
          >{t('factory.reviewTarget')}</label
        ><Dropdown
          id={`${prefix}-target`}
          ariaLabelledby={`${prefix}-target-label`}
          value={targetOptions.some((option) => option.value === stage.target)
            ? stage.target
            : ''}
          options={targetOptions}
          placeholder={t('factory.chooseReviewTarget')}
          invalid={!targetOptions.some(
            (option) => option.value === stage.target,
          )}
          onValueChange={(value) => (stage.target = value)}
        />
      </div>
      <div class="factory-field">
        <label for={`${prefix}-rounds`}>{t('factory.reviewRounds')}</label
        ><TextField
          id={`${prefix}-rounds`}
          type="number"
          min="1"
          max="20"
          step="1"
          aria-describedby={`${prefix}-rounds-help`}
          value={stage.rounds}
          onInput={(value) => (stage.rounds = value)}
        />
        <p id={`${prefix}-rounds-help`} class="factory-help">
          {t('factory.reviewRoundsHelp')}
        </p>
      </div>
    {/if}
    <div class="factory-field factory-field--wide">
      <label for={`${prefix}-instructions`}
        >{t('factory.stageInstructions')}</label
      ><TextArea
        id={`${prefix}-instructions`}
        maxlength="20000"
        rows={3}
        autoGrow
        placeholder={stage.kind === 'review'
          ? t('factory.reviewInstructionsPlaceholder')
          : t('factory.workInstructionsPlaceholder')}
        value={stage.instructions}
        onInput={(value) => (stage.instructions = value)}
      />
    </div>
    <div class="factory-field factory-field--wide">
      <Checkbox
        id={`${prefix}-approval`}
        checked={Boolean(stage.approval)}
        onChange={(value) => (stage.approval = value)}
        >{t('factory.stageApproval')}</Checkbox
      >
    </div>
  </div>
  {#if integrates}<p class="factory-help">{t('factory.integratorHelp')}</p>{/if}

  <div class="factory-lanes">
    <h5>{t('factory.lanes')}</h5>
    {#if stage.lanes.length > 1}<p class="factory-help">
        {t('factory.lanesHelp')}
      </p>{/if}
    {#each stage.lanes as lane, laneIndex (laneIndex)}
      {@const lanePrefix = `${prefix}-${laneIndex}`}
      {@const allOptions = buildModelSelectOptions({
        models,
        modelOnly: true,
        selectedModelValue: lane.model,
        emptyLabel: t('factory.inheritModel'),
      })}
      {@const visibleOptions = filterModelSelectOptions(allOptions, {
        showAll: showAllModels,
        selectedModelValue: lane.model,
      })}
      {@const resolved = resolve(stage, laneIndex)}
      <div class="factory-lane" data-lane={laneIndex}>
        {#if stage.lanes.length > 1}
          <div class="factory-lane-header">
            <span>{t('factory.laneNumber', { lane: laneIndex + 1 })}</span>
            <Button
              variant="tertiary"
              ariaLabel={t('factory.removeLane', {
                lane: laneIndex + 1,
                stage: stageName,
              })}
              onClick={() => stage.lanes.splice(laneIndex, 1)}
              >{t('factory.remove')}</Button
            >
          </div>
        {/if}
        <div class="factory-lane-fields">
          <div class="factory-field">
            <label id={`${lanePrefix}-agent-label`} for={`${lanePrefix}-agent`}
              >{t('factory.agent')}</label
            >
            <Dropdown
              id={`${lanePrefix}-agent`}
              ariaLabelledby={`${lanePrefix}-agent-label`}
              value={agentValue(lane)}
              options={agentOptions(lane)}
              invalid={!lane.agent}
              onOpenChange={model.pickerOpen}
              onValueChange={(value) => selectAgent(lane, value)}
            />
          </div>
          <div class="factory-field">
            <label id={`${lanePrefix}-model-label`} for={`${lanePrefix}-model`}
              >{t('factory.model')}</label
            >
            <SearchableDropdown
              id={`${lanePrefix}-model`}
              ariaLabelledby={`${lanePrefix}-model-label`}
              value={lane.model}
              options={visibleOptions}
              placeholder={t('factory.inheritModel')}
              searchPlaceholder={t('agents.form.modelSearchPlaceholder')}
              emptyLabel={t('agents.form.modelSearchEmpty')}
              footerActionLabel={modelFilterFooterLabel({
                showAll: showAllModels,
                hiddenCount: allOptions.length - visibleOptions.length,
              })}
              onFooterAction={() => (showAllModels = !showAllModels)}
              onOpenChange={model.pickerOpen}
              onValueChange={(value) => selectModel(lane, laneIndex, value)}
            />
          </div>
          <div class="factory-field">
            <label
              id={`${lanePrefix}-effort-label`}
              for={`${lanePrefix}-effort`}>{t('factory.effort')}</label
            >
            <Dropdown
              id={`${lanePrefix}-effort`}
              ariaLabelledby={`${lanePrefix}-effort-label`}
              value={lane.effort}
              options={effortOptions(laneIndex)}
              onValueChange={(value) => (lane.effort = value)}
            />
          </div>
        </div>
        {#if lane.agent && resolved === ''}<p
            class="factory-field-error"
            role="status"
          >
            {t('factory.modelUnresolved')}
          </p>{:else if lane.agent && !lane.model && resolved}<p
            class="factory-help"
            data-effective-model
          >
            {t('factory.effectiveModel', { model: resolved })}
          </p>{/if}
      </div>
    {/each}
    <Button
      variant="tertiary"
      disabled={stage.lanes.length >= MAX_LANES}
      onClick={addLane}>{t('factory.addLane')}</Button
    >
  </div>
  {#if sameModel}<p class="factory-stage-warning" role="status">
      {t('factory.sameModel')}
    </p>{/if}
</article>

<style>
  .factory-stage {
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface-2);
    padding: 16px;
    min-width: 0;
  }
  .factory-stage[data-stage-kind='review'] {
    border-left: 3px solid var(--accent);
  }
  .factory-stage-header {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .factory-stage-header h4 {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-size: 13px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .factory-stage-header h4 .factory-subtle {
    font-weight: 400;
    font-size: 12px;
  }
  .factory-stage-number {
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
  .factory-stage-tools {
    display: flex;
    gap: 2px;
  }
  .factory-lanes {
    margin-top: 18px;
    display: grid;
    gap: 12px;
  }
  .factory-lanes h5 {
    margin: 0;
    font-size: 12px;
  }
  .factory-lanes > :global(button) {
    justify-self: start;
  }
  .factory-lane {
    border-top: 1px solid var(--border);
    padding-top: 12px;
    display: grid;
    gap: 8px;
    min-width: 0;
  }
  .factory-lane-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    color: var(--text-med);
  }
  .factory-lane-fields {
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(0, 1.2fr) minmax(0, 1fr);
    gap: 12px;
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
  .factory-stage-warning {
    margin: 14px 0 0;
    border-left: 2px solid var(--amber);
    padding-left: 8px;
    font-size: 12px;
    line-height: 1.5;
  }
  @media (max-width: 720px) {
    .factory-lane-fields {
      grid-template-columns: 1fr;
    }
  }
</style>
