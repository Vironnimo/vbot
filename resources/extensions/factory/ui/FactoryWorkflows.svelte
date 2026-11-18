<script module>
  const loadEditor = () => import('./WorkflowEditor.svelte');
</script>

<script>
  import { t } from '@vbot/extension-sdk/lib/i18n.js';
  import Button from '@vbot/extension-sdk/ui/Button.svelte';
  import Dropdown from '@vbot/extension-sdk/ui/Dropdown.svelte';
  import EmptyState from '@vbot/extension-sdk/ui/EmptyState.svelte';
  import LazyContent from '@vbot/extension-sdk/ui/LazyContent.svelte';
  import { projectName } from './presentation.js';

  // Every Workflow in one list; the selected one, or a new one, beside it.
  let { model, onNavigate } = $props();
  const creating = $derived(model.workflowId === 'new');
  const selected = $derived(
    creating
      ? null
      : model.workflows.find((item) => item.id === model.workflowId),
  );
  const newOptions = $derived([
    {
      value: 'blank',
      label: t('factory.blankWorkflow'),
      secondaryLabel: t('factory.blankWorkflowHelp'),
    },
    ...model.templates.map((template) => ({
      value: template.id,
      label: template.name,
      secondaryLabel: template.description,
      group: t('factory.templates'),
    })),
    ...model.workflows.map((workflow) => ({
      value: `copy:${workflow.id}`,
      label: t('factory.copyName', { name: workflow.name }),
      group: t('factory.copyWorkflow'),
    })),
  ]);

  function startNew(choice) {
    model.newTemplate = choice;
    void onNavigate('/workflows/new');
  }

  function summary(workflow) {
    const parts = [
      workflow.enabled ? t('factory.enabled') : t('factory.paused'),
      workflow.project_id
        ? projectName(model.catalog, workflow.project_id)
        : t('factory.anyProject'),
    ];
    if (workflow.triggers?.length)
      parts.push(
        t('factory.triggerCount', { count: workflow.triggers.length }),
      );
    return parts.join(' · ');
  }
</script>

<div class="factory-workflows-page">
  <nav class="factory-workflow-list" aria-label={t('factory.workflows')}>
    <h2>{t('factory.workflows')}</h2>
    <Button variant="tertiary" onClick={() => onNavigate('/cards')}
      >{t('factory.native.workflowCards')}</Button
    >
    <div class="factory-new-workflow">
      <Dropdown
        id="factory-new-workflow"
        ariaLabel={t('factory.addWorkflow')}
        value=""
        options={newOptions}
        placeholder={t('factory.addWorkflow')}
        onOpenChange={model.pickerOpen}
        onValueChange={startNew}
      />
    </div>
    {#each model.workflows as workflow (workflow.id)}
      <button
        class="factory-workflow-choice"
        class:factory-workflow-choice--selected={model.workflowId ===
          workflow.id}
        aria-current={model.workflowId === workflow.id ? 'page' : undefined}
        data-workflow-id={workflow.id}
        onclick={() =>
          onNavigate(`/workflows/${encodeURIComponent(workflow.id)}`)}
      >
        <span>{workflow.name}</span><span class="factory-help"
          >{summary(workflow)}</span
        >
      </button>
    {:else}<p class="factory-help">{t('factory.noWorkflows')}</p>{/each}
  </nav>
  <div class="factory-workflow-content">
    {#if creating}
      {#key `new:${model.newTemplate}`}<LazyContent load={loadEditor}
          >{#snippet children(Component)}<Component
              {model}
              {onNavigate}
              workflow={null}
            />{/snippet}</LazyContent
        >{/key}
    {:else if selected && !model.detailLoading}
      {#key selected.id}<LazyContent load={loadEditor}
          >{#snippet children(Component)}<Component
              {model}
              {onNavigate}
              workflow={model.selectedWorkflow?.id === selected.id
                ? model.selectedWorkflow
                : selected}
            />{/snippet}</LazyContent
        >{/key}
    {:else if model.detailLoading}<p role="status">{t('common.loading')}</p>
    {:else}<EmptyState
        title={t('factory.chooseWorkflow')}
        description={t('factory.workflowsHelp')}
      />{/if}
  </div>
</div>

<style>
  .factory-new-workflow {
    margin: 0 0 10px;
  }
</style>
