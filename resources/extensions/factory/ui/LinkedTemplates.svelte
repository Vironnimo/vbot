<script>
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import WorkflowDiagram from './WorkflowDiagram.svelte';
  import { copy, failureText } from './presentation.js';
  import { changeField, changeValue } from './library.js';

  let { model, workflow, onReplace } = $props();
  let reviewing = $state(null);
  let preview = $state(null);
  let resolutions = $state({});
  let busy = $state(false);
  let error = $state('');
  let request = 0;
  const sources = $derived(workflow.sources ?? []);
  const choices = [
    { value: '', label: t('factory.library.chooseResolution') },
    { value: 'local', label: t('factory.library.keepLocal') },
    { value: 'template', label: t('factory.library.useTemplate') },
  ];
  const canApply = $derived(
    preview?.valid &&
      preview.conflicts.every((conflict) => Boolean(conflict.resolution)),
  );
  const label = (change) => {
    const step =
      reviewing?.workflow.steps.find((item) => item.id === change.step) ??
      preview?.workflow.steps.find((item) => item.id === change.step);
    return step
      ? `${step.name} · ${changeField(change.field)}`
      : changeField(change.field);
  };

  async function inspect(source) {
    const item = model.library.find((entry) => entry.id === source.template_id);
    if (!item) return;
    reviewing = {
      source_id: source.id,
      version: item.latest_version ?? item.version,
      workflow: copy(workflow),
      name: item.name,
    };
    resolutions = {};
    preview = null;
    await refreshPreview();
  }

  async function refreshPreview() {
    const current = ++request;
    busy = true;
    error = '';
    try {
      const result = await model.call('library.preview_update', {
        workflow: reviewing.workflow,
        source_id: reviewing.source_id,
        version: reviewing.version,
        resolutions: copy(resolutions),
      });
      if (current === request) preview = result;
    } catch (cause) {
      if (current === request) error = failureText(cause);
    } finally {
      if (current === request) busy = false;
    }
  }

  async function apply() {
    if (!canApply || busy) return;
    busy = true;
    try {
      const result = await model.call('library.apply_update', {
        workflow: reviewing.workflow,
        source_id: reviewing.source_id,
        version: reviewing.version,
        resolutions: copy(resolutions),
      });
      if (await onReplace(result.workflow, reviewing.workflow))
        reviewing = null;
      else error = t('factory.library.draftChanged');
    } catch (cause) {
      error = failureText(cause);
    } finally {
      busy = false;
    }
  }

  function close() {
    request += 1;
    reviewing = null;
    busy = false;
  }
</script>

{#if sources.length}
  <section
    class="factory-linked-templates"
    aria-label={t('factory.library.linkedTemplates')}
  >
    <h4>{t('factory.library.linkedTemplates')}</h4>
    {#each sources as source (source.id)}
      {@const item = model.library.find(
        (entry) => entry.id === source.template_id,
      )}
      {@const latest = item?.latest_version ?? item?.version ?? source.version}
      <div class="factory-linked-template" data-source-id={source.id}>
        <span
          ><strong>{item?.name ?? source.base.name}</strong> · {t(
            'factory.library.version',
            { version: source.version },
          )}{item?.archived ? ` · ${t('factory.library.archived')}` : ''}</span
        >
        {#if latest > source.version}<Button
            variant="secondary"
            onClick={() => inspect(source)}
            >{t('factory.library.reviewUpdate', { version: latest })}</Button
          >
        {:else}<span class="factory-help">{t('factory.library.upToDate')}</span
          >{/if}
      </div>
    {/each}
  </section>
{/if}

{#if reviewing}
  <Modal
    title={t('factory.library.updateTitle', {
      name: reviewing.name,
      version: reviewing.version,
    })}
    size="lg"
    closeDisabled={busy}
    onClose={close}
  >
    {#snippet body()}<div class="modal-body factory-form">
        <p class="factory-help">{t('factory.library.updateHelp')}</p>
        {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
        {#if preview}
          {#if preview.conflicts.length}<h3>
              {t('factory.library.conflicts')}
            </h3>{/if}
          {#each preview.conflicts as conflict, index (conflict.id)}
            <section
              class="factory-update-conflict"
              data-conflict-id={conflict.id}
            >
              <h4>{label(conflict)}</h4>
              <div class="factory-update-values">
                <div>
                  <strong>{t('factory.library.localValue')}</strong>
                  <pre>{changeValue(conflict.local, reviewing.workflow)}</pre>
                </div>
                <div>
                  <strong>{t('factory.library.templateValue')}</strong>
                  <pre>{changeValue(conflict.template, preview.workflow)}</pre>
                </div>
              </div>
              <details>
                <summary>{t('factory.library.baseValue')}</summary>
                <pre>{changeValue(conflict.base, reviewing.workflow)}</pre>
              </details>
              <Dropdown
                id={`factory-conflict-${index}`}
                ariaLabel={t('factory.library.resolveField', {
                  field: label(conflict),
                })}
                value={resolutions[conflict.id] ?? ''}
                options={choices}
                disabled={busy}
                onValueChange={(value) => {
                  if (value) resolutions[conflict.id] = value;
                  else delete resolutions[conflict.id];
                  void refreshPreview();
                }}
              />
            </section>
          {/each}
          <h3>{t('factory.library.changes')}</h3>
          {#each preview.changes as change, index (`${change.step}:${change.field}:${index}`)}
            <details class="factory-update-change">
              <summary>{label(change)}</summary>
              <div class="factory-update-values">
                <div>
                  <strong>{t('factory.library.baseValue')}</strong>
                  <pre>{changeValue(change.base, reviewing.workflow)}</pre>
                </div>
                <div>
                  <strong>{t('factory.library.templateValue')}</strong>
                  <pre>{changeValue(change.template, preview.workflow)}</pre>
                </div>
              </div>
            </details>
          {:else}<p>{t('factory.library.noChanges')}</p>{/each}
          {#each preview.problems ?? [] as problem, index (index)}<Banner
              variant="warn">{problem}</Banner
            >{/each}
          {#if preview.valid}<WorkflowDiagram
              workflow={preview.workflow}
            />{/if}
        {:else if busy}<p role="status">{t('common.loading')}</p>{/if}
      </div>{/snippet}
    {#snippet footer()}<Button
        variant="secondary"
        disabled={busy}
        onClick={close}>{t('common.cancel')}</Button
      ><Button
        variant="primary"
        disabled={!canApply}
        loading={busy}
        onClick={apply}>{t('factory.library.applyUpdate')}</Button
      >{/snippet}
  </Modal>
{/if}

<style>
  .factory-linked-templates {
    margin: 16px 0;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
  }
  h4 {
    margin: 0 0 10px;
    font-size: 13px;
  }
  .factory-linked-template {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    padding: 6px 0;
  }
  .factory-update-conflict {
    padding: 14px;
    border: 1px solid var(--amber);
    border-radius: 8px;
    display: grid;
    gap: 10px;
  }
  .factory-update-values {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .factory-update-values > div {
    min-width: 0;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-family: inherit;
    font-size: 12px;
    max-height: 240px;
    overflow: auto;
  }
  .factory-update-change {
    border-bottom: 1px solid var(--border);
    padding: 10px 0;
  }
  @media (max-width: 600px) {
    .factory-update-values {
      grid-template-columns: 1fr;
    }
  }
</style>
