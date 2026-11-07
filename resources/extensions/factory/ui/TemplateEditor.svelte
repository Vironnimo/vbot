<script>
  import { untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ConfirmDialog from '../../../../webui/src/components/ui/ConfirmDialog.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import WorkflowDiagram from './WorkflowDiagram.svelte';
  import WorkflowStep from './WorkflowStep.svelte';
  import { copy, failureText } from './presentation.js';
  import {
    MAX_STEPS,
    canonicalWorkflows,
    moveStep,
    newStep,
    newWorkflow,
    removeStep,
    workflowProblem,
  } from './workflows.js';
  import { templateKinds } from './library.js';

  let { model, record = null, onNavigate } = $props();
  const key = untrack(() => model.templateId);
  const initial = untrack(
    () =>
      model.getTemplateDraft(key) ?? {
        name: record?.name ?? '',
        description: record?.description ?? '',
        kind: record?.kind ?? 'workflow',
        workflow: copy(record?.workflow ?? newWorkflow([])),
        revision: record?.revision ?? 0,
      },
  );
  let draft = $state(initial);
  let baseline = $state(
    untrack(() =>
      JSON.stringify(
        record
          ? {
              name: record.name,
              description: record.description,
              kind: record.kind,
              workflow: record.workflow,
              revision: record.revision,
            }
          : initial,
      ),
    ),
  );
  let selectedId = $state('');
  let busy = $state(false);
  let created = $state(false);
  let error = $state('');
  let confirmArchive = $state(false);
  let historyVersion = $state('');
  let history = $state(null);
  const selectedIndex = $derived(
    Math.max(
      0,
      draft.workflow.steps.findIndex((step) => step.id === selectedId),
    ),
  );
  const changed = $derived(JSON.stringify(draft) !== baseline || !record);
  const newer = $derived(Boolean(record && record.revision > draft.revision));
  const kinds = templateKinds();
  const globalProject = { name: t('factory.library'), agent_profiles: [] };

  $effect(() => {
    const saved = $state.snapshot(draft);
    if (!created) untrack(() => model.keepTemplateDraft(key, saved));
  });

  function reset() {
    draft = {
      name: record?.name ?? '',
      description: record?.description ?? '',
      kind: record?.kind ?? 'workflow',
      workflow: copy(record?.workflow ?? newWorkflow([])),
      revision: record?.revision ?? 0,
    };
    baseline = JSON.stringify(draft);
    error = '';
  }

  async function publish() {
    if (busy) return;
    error = !draft.name.trim()
      ? t('factory.library.nameRequired')
      : workflowProblem([{ ...draft.workflow, name: draft.name }], {
          requireAgents: false,
        });
    if (!error && draft.kind === 'step' && draft.workflow.steps.length !== 1)
      error = t('factory.library.singleStepRequired');
    if (error) return;
    const submitted = JSON.stringify(draft);
    busy = true;
    try {
      const result = await model.call('library.publish', {
        ...(record ? { id: record.id } : {}),
        expected_revision: draft.revision,
        name: draft.name.trim(),
        description: draft.description,
        kind: draft.kind,
        workflow: canonicalWorkflows([
          {
            ...draft.workflow,
            name: draft.name.trim(),
            description: draft.description,
          },
        ])[0],
      });
      model.acceptSavedTemplate(result.template);
      const publishedDraft = {
        name: result.template.name,
        description: result.template.description,
        kind: result.template.kind,
        workflow: copy(result.template.workflow),
        revision: result.template.revision,
      };
      if (JSON.stringify(draft) === submitted) draft = publishedDraft;
      else draft.revision = result.template.revision;
      baseline = JSON.stringify(publishedDraft);
      model.keepTemplateDraft(result.template.id, copy(draft));
      if (!record) {
        created = true;
        model.clearTemplateDraft('new');
        if (model.templateId === key)
          await onNavigate(
            `/library/${encodeURIComponent(result.template.id)}`,
          );
      }
    } catch (cause) {
      error = failureText(cause);
    } finally {
      busy = false;
    }
  }

  async function archive() {
    const revision = record.revision;
    busy = true;
    try {
      const result = await model.call('library.archive', {
        id: record.id,
        expected_revision: revision,
        archived: !record.archived,
      });
      model.acceptSavedTemplate(result.template);
      if (draft.revision === revision)
        draft.revision = result.template.revision;
      const previous = JSON.parse(baseline);
      if (previous.revision === revision)
        baseline = JSON.stringify({
          ...previous,
          revision: result.template.revision,
        });
      confirmArchive = false;
    } catch (cause) {
      error = failureText(cause);
    } finally {
      busy = false;
    }
  }

  async function inspectVersion(value) {
    historyVersion = value;
    history = null;
    if (!value) return;
    try {
      const result = await model.call('library.get', {
        id: record.id,
        version: Number(value),
      });
      if (historyVersion === value) history = result.template;
    } catch (cause) {
      error = failureText(cause);
    }
  }

  function add() {
    const step = newStep(draft.workflow.steps);
    draft.workflow.steps.push(step);
    selectedId = step.id;
  }
</script>

<article class="factory-template-editor" data-template-id={record?.id ?? 'new'}>
  <h2>{record?.name ?? t('factory.library.new')}</h2>
  <p class="factory-help">{t('factory.library.publishHelp')}</p>
  {#if newer}<Banner variant="warn"
      >{t('factory.library.newerDraft')}<Button
        variant="tertiary"
        onClick={reset}>{t('factory.library.reload')}</Button
      ></Banner
    >{/if}
  {#if record?.archived}<Banner variant="warn"
      >{t('factory.library.archivedHelp')}</Banner
    >{/if}
  <div class="factory-fields">
    <div class="factory-field">
      <label for="factory-template-name">{t('factory.library.name')}</label
      ><TextField
        id="factory-template-name"
        maxlength="80"
        value={draft.name}
        onInput={(value) => (draft.name = value)}
      />
    </div>
    <div class="factory-field">
      <label for="factory-template-kind">{t('factory.library.kind')}</label
      ><Dropdown
        id="factory-template-kind"
        ariaLabel={t('factory.library.kind')}
        value={draft.kind}
        options={kinds}
        disabled={Boolean(record)}
        onValueChange={(value) => (draft.kind = value)}
      />
    </div>
    <div class="factory-field factory-field--wide">
      <label for="factory-template-description"
        >{t('factory.workflowDescription')}</label
      ><TextArea
        id="factory-template-description"
        maxlength="2000"
        rows={2}
        value={draft.description}
        onInput={(value) => (draft.description = value)}
      />
    </div>
  </div>
  <p class="factory-help">{t('factory.library.agentHelp')}</p>
  <WorkflowDiagram
    workflow={draft.workflow}
    selectedId={draft.workflow.steps[selectedIndex]?.id ?? ''}
    onSelect={(id) => (selectedId = id)}
  />
  <div class="factory-actions">
    <Button
      variant="secondary"
      disabled={draft.workflow.steps.length >= MAX_STEPS ||
        draft.kind === 'step'}
      onClick={add}>{t('factory.addStep')}</Button
    >
  </div>
  {#if draft.workflow.steps[selectedIndex]}
    {#key draft.workflow.steps[selectedIndex].id}<WorkflowStep
        workflow={draft.workflow}
        bind:step={draft.workflow.steps[selectedIndex]}
        index={selectedIndex}
        project={globalProject}
        {model}
        requireAgent={false}
        resolve={(step) => step.model || undefined}
        onMove={(delta) => {
          selectedId = draft.workflow.steps[selectedIndex].id;
          draft.workflow.steps = moveStep(
            draft.workflow.steps,
            selectedIndex,
            delta,
          );
        }}
        onRemove={() => {
          draft.workflow.steps = removeStep(
            draft.workflow.steps,
            selectedIndex,
          );
          selectedId =
            draft.workflow.steps[Math.max(0, selectedIndex - 1)]?.id ?? '';
        }}
      />{/key}
  {/if}
  {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
  <div class="factory-actions">
    <Button
      variant="primary"
      loading={busy}
      disabled={!changed || Boolean(record?.archived)}
      onClick={publish}
      >{record
        ? t('factory.library.publishVersion')
        : t('factory.library.publish')}</Button
    >
    <Button variant="tertiary" disabled={busy} onClick={reset}
      >{t('factory.library.discardDraft')}</Button
    >
    {#if record}<Button
        variant="tertiary"
        disabled={busy}
        onClick={() => (confirmArchive = true)}
        >{record.archived
          ? t('factory.library.restore')
          : t('factory.library.archive')}</Button
      >{/if}
  </div>
  {#if record}
    <section class="factory-section">
      <h3>{t('factory.library.history')}</h3>
      <Dropdown
        id="factory-template-history"
        ariaLabel={t('factory.library.history')}
        value={historyVersion}
        options={[
          { value: '', label: t('factory.library.selectVersion') },
          ...(record.versions ?? [record.version]).map((version) => ({
            value: String(version),
            label: t('factory.library.version', { version }),
          })),
        ]}
        onValueChange={inspectVersion}
      />
      {#if history}<h4>
          {history.name} · {t('factory.library.version', {
            version: history.version,
          })}
        </h4>
        <p>{history.description}</p>
        <WorkflowDiagram workflow={history.workflow} />
        {#each history.workflow.steps as step (step.id)}<details>
            <summary>{step.name}</summary>
            <p class="factory-prose">{step.task}</p>
          </details>{/each}
      {/if}
    </section>
  {/if}
</article>
{#if confirmArchive}<ConfirmDialog
    title={record.archived
      ? t('factory.library.restore')
      : t('factory.library.archive')}
    body={t('factory.library.archiveHelp')}
    confirmLabel={record.archived
      ? t('factory.library.restore')
      : t('factory.library.archive')}
    {busy}
    {error}
    onConfirm={archive}
    onCancel={() => (confirmArchive = false)}
  />{/if}

<style>
  .factory-template-editor {
    display: grid;
    gap: 16px;
  }
  h2 {
    margin: 0;
  }
  .factory-field--wide {
    grid-column: 1 / -1;
  }
</style>
