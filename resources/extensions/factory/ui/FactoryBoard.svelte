<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import SortableList from '../../../../webui/src/components/ui/SortableList.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import Toggle from '../../../../webui/src/components/ui/Toggle.svelte';
  import {
    boardColumns,
    stateLabel,
    queuedActionLabel,
    landingStatusLabel,
    waitingLabel,
    originLabel,
    projectName,
  } from './presentation.js';

  let { model, onNavigate } = $props();
  let createOpen = $state(false);
  let projectId = $state('');
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
  const enabled = $derived(
    projects.filter((project) =>
      model.setups.some(
        (setup) => setup.project_id === project.id && setup.enabled,
      ),
    ),
  );
  const createOptions = $derived(
    enabled.map((project) => ({ value: project.id, label: project.name })),
  );

  function newCard() {
    projectId ||= enabled.some((project) => project.id === model.projectFilter)
      ? model.projectFilter
      : (enabled[0]?.id ?? '');
    createError = '';
    createOpen = true;
  }

  async function createCard() {
    createError = !projectId
      ? t('factory.projectRequired')
      : !title.trim()
        ? t('factory.titleRequired')
        : !description.trim()
          ? t('factory.descriptionRequired')
          : '';
    if (createError) return;
    const result = await model.mutate('card.create', {
      project_id: projectId,
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
  <Button
    class="factory-inbox-count"
    variant="tertiary"
    disabled={!waiting}
    onClick={() =>
      boardElement
        ?.querySelector('[data-column="waiting"]')
        ?.scrollIntoView({ block: 'nearest', inline: 'center' })}
    >{t('factory.waitingCount', { count: waiting })}</Button
  >
  <span class="factory-switch-label"
    >{t('factory.showDiscarded')}<Toggle
      checked={model.showDiscarded}
      ariaLabel={t('factory.showDiscarded')}
      onChange={(value) => (model.showDiscarded = value)}
    /></span
  >
  <Button variant="primary" onClick={newCard}>{t('factory.newCard')}</Button>
</div>
<div
  class="factory-board"
  aria-label={t('factory.board')}
  bind:this={boardElement}
>
  {#each columns as column (column.id)}
    <section
      class="factory-column"
      class:factory-column--waiting={column.id === 'waiting'}
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
  <button
    class="factory-card"
    data-card-id={card.id}
    onclick={() => onNavigate(`/card/${encodeURIComponent(card.id)}`)}
  >
    <span class="factory-card-project"
      >{projectName(model.catalog, card.project_id)}</span
    >
    <span class="factory-card-title">{card.title}</span>
    {#if card.integration_landing}<span
        class="factory-card-landing-status"
        data-landing-status={card.integration_landing.status}
        >{!card.integration_landing.base_sha ||
        card.integration_landing.head_sha === card.head_sha
          ? landingStatusLabel(card.integration_landing.status)
          : t('factory.landing.alreadyLanded')}</span
      >{/if}
    {#if card.state === 'waiting'}<span class="factory-card-reason"
        >{waitingLabel(card.waiting_reason)}</span
      >{/if}
    {#if card.queued_action}<span class="factory-card-reason"
        >{queuedActionLabel(card.queued_action)}</span
      >{/if}
    {#if card.recommendation && card.state === 'waiting'}<span
        class="factory-card-preview">{card.recommendation}</span
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
            >{t('factory.noEnabledProjects')}<Button
              variant="tertiary"
              onClick={() => {
                createOpen = false;
                void onNavigate('/setups');
              }}>{t('factory.setups')}</Button
            ></Banner
          >{/if}
        <div class="factory-field">
          <label for="factory-create-project">{t('factory.project')}</label
          ><Dropdown
            id="factory-create-project"
            value={projectId}
            options={createOptions}
            placeholder={t('factory.selectProject')}
            ariaLabel={t('factory.project')}
            disabled={Boolean(model.pending)}
            onOpenChange={model.pickerOpen}
            onValueChange={(value) => (projectId = value)}
          />
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
