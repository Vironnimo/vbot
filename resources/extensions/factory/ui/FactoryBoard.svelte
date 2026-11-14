<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import EmptyState from '../../../../webui/src/components/ui/EmptyState.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import SortableList from '../../../../webui/src/components/ui/SortableList.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import {
    boardColumns,
    stateLabel,
    queuedActionLabel,
    waitingLabel,
    originLabel,
    projectName,
    cardWorkflow,
    activeSteps,
  } from './presentation.js';

  let { model, onNavigate } = $props();
  let createOpen = $state(false);
  let projectId = $state('');
  let workflowId = $state('');
  let title = $state('');
  let description = $state('');
  let createError = $state('');
  let boardElement = $state(null);
  const columns = $derived(boardColumns(model.cards, model.showDiscarded));
  const waiting = $derived(
    model.cards.filter((card) => card.state === 'waiting').length,
  );
  const projects = $derived(model.catalog.projects ?? []);
  const options = $derived([
    { value: '', label: t('factory.allProjects') },
    ...projects.map((project) => ({ value: project.id, label: project.name })),
  ]);
  const enabled = $derived(model.workflows.filter((item) => item.enabled));
  const workflowOptions = $derived(
    enabled.map((workflow) => ({
      value: workflow.id,
      label: workflow.name,
      secondaryLabel: workflow.description,
    })),
  );
  const createWorkflow = $derived(
    enabled.find((workflow) => workflow.id === workflowId),
  );
  // A Workflow with a Project of its own always works there.
  const fixedProject = $derived(createWorkflow?.project_id ?? '');
  const createProjectOptions = $derived([
    { value: '', label: t('factory.noProject') },
    ...projects.map((project) => ({ value: project.id, label: project.name })),
  ]);

  function newCard() {
    if (!enabled.some((workflow) => workflow.id === workflowId))
      workflowId = enabled[0]?.id ?? '';
    if (!projectId && projects.some((item) => item.id === model.projectFilter))
      projectId = model.projectFilter;
    createError = '';
    createOpen = true;
  }

  async function createCard() {
    createError = !workflowId
      ? t('factory.workflowRequired')
      : !title.trim()
        ? t('factory.titleRequired')
        : !description.trim()
          ? t('factory.descriptionRequired')
          : '';
    if (createError) return;
    const project = fixedProject || projectId;
    const result = await model.mutate('card.create', {
      workflow: workflowId,
      ...(project ? { project_id: project } : {}),
      title: title.trim(),
      description: description.trim(),
    });
    if (!result) return;
    createOpen = false;
    title = '';
    description = '';
    await onNavigate(`/card/${encodeURIComponent(result.card.id)}`);
  }

  async function reorder(from, to) {
    const backlog =
      columns.find((column) => column.id === 'backlog')?.cards ?? [];
    if (
      from < 0 ||
      to < 0 ||
      to >= backlog.length ||
      from === to ||
      model.pending
    )
      return;
    await model.mutate('card.move', {
      id: backlog[from].id,
      position: backlog[to].position,
    });
  }
</script>

<div class="factory-board-toolbar">
  <div class="factory-filter">
    <Dropdown
      id="factory-project-filter"
      ariaLabel={t('factory.project')}
      value={model.projectFilter}
      {options}
      onOpenChange={model.pickerOpen}
      onValueChange={model.filterProject}
    />
  </div>
  {#if waiting}<Button
      class="factory-inbox-count"
      variant="tertiary"
      onClick={() =>
        boardElement
          ?.querySelector('[data-column="waiting"]')
          ?.scrollIntoView({ block: 'nearest', inline: 'center' })}
      >{t('factory.waitingCount', { count: waiting })}</Button
    >{/if}
  <span class="factory-switch-label"
    >{t('factory.showDiscarded')}<Toggle
      checked={model.showDiscarded}
      ariaLabel={t('factory.showDiscarded')}
      onChange={(value) => (model.showDiscarded = value)}
    /></span
  >
  <Button variant="primary" onClick={newCard}>{t('factory.newCard')}</Button>
</div>
{#if !model.workflows.length && !model.cards.length}<EmptyState
    class="factory-board-empty"
    title={t('factory.boardEmpty')}
    description={t('factory.boardEmptyHelp')}
    >{#snippet actions()}<Button
        variant="primary"
        onClick={() => onNavigate('/workflows')}
        >{t('factory.createFirstWorkflow')}</Button
      >{/snippet}</EmptyState
  >{/if}
<div
  class="factory-board"
  aria-label={t('factory.board')}
  bind:this={boardElement}
>
  {#each columns as column (column.id)}
    <section
      class="factory-column"
      class:factory-column--waiting={column.id === 'waiting' &&
        column.cards.length > 0}
      data-column={column.id}
      aria-labelledby={`factory-column-${column.id}`}
    >
      <header>
        <h2 id={`factory-column-${column.id}`}>{column.label}</h2>
        <span class="factory-count">{column.cards.length}</span>
      </header>
      {#if column.id === 'backlog' && column.cards.length}
        <SortableList
          items={column.cards}
          getLabel={(card) => card.title}
          onReorder={reorder}
          disabled={Boolean(model.pending)}
          class="factory-card-list"
          itemClass="factory-sortable-card"
        >
          {#snippet item(card, index)}
            {@render cardTile(card)}
            <div class="factory-reorder" data-sortable-ignore>
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('factory.moveUp', { title: card.title })}
                disabled={index === 0 || Boolean(model.pending)}
                onClick={() => reorder(index, index - 1)}
                ><span aria-hidden="true">↑</span></Button
              >
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('factory.moveDown', { title: card.title })}
                disabled={index === column.cards.length - 1 ||
                  Boolean(model.pending)}
                onClick={() => reorder(index, index + 1)}
                ><span aria-hidden="true">↓</span></Button
              >
            </div>
          {/snippet}
        </SortableList>
      {:else}
        <ul class="factory-card-list">
          {#each column.cards as card (card.id)}<li>
              {@render cardTile(card)}
            </li>{:else}<li class="factory-column-empty">
              {t('factory.emptyColumn')}
            </li>{/each}
        </ul>
      {/if}
    </section>
  {/each}
</div>

{#snippet cardTile(card)}
  {@const workflow = cardWorkflow(card, model.workflows)}
  {@const active = activeSteps(card)}
  <button
    class="factory-card"
    data-card-id={card.id}
    onclick={() => onNavigate(`/card/${encodeURIComponent(card.id)}`)}
  >
    <span class="factory-card-project"
      >{card.project_id
        ? projectName(model.catalog, card.project_id)
        : t('factory.noProject')}</span
    >
    <span class="factory-card-title">{card.title}</span>
    {#if workflow}<span class="factory-card-step" data-card-step
        ><span>{workflow.name}</span>{#if active.length}<span aria-hidden="true"
            >·</span
          ><span>{active.map((step) => step.name).join(', ')}</span>{/if}</span
      >{/if}
    {#if card.state === 'waiting'}<span class="factory-card-reason"
        >{waitingLabel(card.waiting_reason)}</span
      >{/if}
    {#if card.queued_action}<span class="factory-card-reason"
        >{queuedActionLabel(card.queued_action)}</span
      >{/if}
    {#if card.summary && card.state === 'waiting'}<span
        class="factory-card-preview">{card.summary}</span
      >{/if}
    <span class="factory-card-meta"
      ><span>{stateLabel(card.state)}</span><span class="factory-origin-badge"
        >{originLabel(card.origin)}</span
      ></span
    >
  </button>
{/snippet}

{#if createOpen}
  <Modal
    title={t('factory.newCard')}
    onClose={() => (createOpen = false)}
    closeDisabled={Boolean(model.pending)}
  >
    {#snippet body()}
      <div class="modal-body factory-form">
        <p class="factory-help">{t('factory.createHelp')}</p>
        {#if !enabled.length}<Banner variant="warn"
            >{t('factory.noEnabledWorkflows')}<Button
              variant="tertiary"
              onClick={() => {
                createOpen = false;
                void onNavigate('/workflows');
              }}>{t('factory.workflows')}</Button
            ></Banner
          >{/if}
        <div class="factory-field">
          <label
            id="factory-create-workflow-label"
            for="factory-create-workflow">{t('factory.workflow')}</label
          ><Dropdown
            id="factory-create-workflow"
            ariaLabelledby="factory-create-workflow-label"
            value={workflowId}
            options={workflowOptions}
            placeholder={t('factory.selectWorkflow')}
            disabled={Boolean(model.pending)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (workflowId = value)}
          />
        </div>
        <div class="factory-field">
          <label id="factory-create-project-label" for="factory-create-project"
            >{t('factory.project')}</label
          ><Dropdown
            id="factory-create-project"
            ariaLabelledby="factory-create-project-label"
            value={fixedProject || projectId}
            options={createProjectOptions}
            disabled={Boolean(model.pending) || Boolean(fixedProject)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (projectId = value)}
          />
          {#if fixedProject}<span class="factory-help"
              >{t('factory.workflowFixesProject')}</span
            >{/if}
        </div>
        <div class="factory-field">
          <label for="factory-create-title">{t('factory.cardTitle')}</label
          ><TextField
            id="factory-create-title"
            maxlength="200"
            value={title}
            disabled={Boolean(model.pending)}
            onInput={(value) => (title = value)}
          />
        </div>
        <div class="factory-field">
          <label for="factory-create-description"
            >{t('factory.description')}</label
          ><TextArea
            id="factory-create-description"
            maxlength="20000"
            value={description}
            rows={5}
            disabled={Boolean(model.pending)}
            onInput={(value) => (description = value)}
          />
        </div>
        {#if createError || model.error}<Banner variant="error" role="alert"
            >{createError || model.error}</Banner
          >{/if}
      </div>
    {/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={Boolean(model.pending)}
        onClick={() => (createOpen = false)}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        disabled={!enabled.length}
        loading={Boolean(model.pending)}
        onClick={createCard}>{t('factory.createCard')}</Button
      >{/snippet}
  </Modal>
{/if}
