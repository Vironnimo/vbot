<script>
  import { untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Checkbox from '../../../../webui/src/components/ui/Checkbox.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';
  import { copy, failureText } from './presentation.js';
  import { templateKinds } from './library.js';

  let { model, workflow, onLinked, onClose } = $props();
  const snapshot = untrack(() => copy(workflow));
  let name = $state(snapshot.name);
  let description = $state(snapshot.description);
  let kind = $state('workflow');
  let selected = $state(snapshot.steps.map((step) => step.id));
  let targetId = $state('');
  let sourceId = $state('');
  let busy = $state(false);
  let error = $state('');
  let published = $state(null);
  const kinds = templateKinds();
  const targets = $derived(
    model.library.filter((item) => !item.archived && item.kind === kind),
  );
  const sources = $derived(
    (snapshot.sources ?? []).filter((entry) => entry.template_id === targetId),
  );
  const source = $derived(
    sources.length === 1
      ? sources[0]
      : sources.find((entry) => entry.id === sourceId),
  );
  const selectedIds = $derived(
    kind === 'workflow' ? snapshot.steps.map((step) => step.id) : selected,
  );
  const overlaps = $derived(
    (snapshot.sources ?? [])
      .map((entry) =>
        Object.values(entry.step_map).filter((id) =>
          snapshot.steps.some((step) => step.id === id),
        ),
      )
      .filter((members) => members.some((id) => selectedIds.includes(id))),
  );
  const partialOverlap = $derived(
    overlaps.some((members) => members.some((id) => !selectedIds.includes(id))),
  );

  function toggle(id, checked) {
    selected = snapshot.steps
      .map((step) => step.id)
      .filter((step) => (step === id ? checked : selected.includes(step)));
  }

  async function publish() {
    if (busy || published) return;
    error = !name.trim()
      ? t('factory.library.nameRequired')
      : !targetId && partialOverlap
        ? t('factory.library.partialReplacement')
        : sources.length > 1 && !source
          ? t('factory.library.selectSource')
          : kind !== 'workflow' && !selected.length
            ? t('factory.library.selectSteps')
            : kind === 'step' && selected.length !== 1
              ? t('factory.library.singleStepRequired')
              : '';
    if (error) return;
    busy = true;
    try {
      const target = targetId
        ? (await model.call('library.get', { id: targetId })).template
        : null;
      const result = await model.call('library.extract', {
        workflow: snapshot,
        ...(kind === 'workflow' ? {} : { step_ids: selected }),
        ...(source ? { source_id: source.id } : {}),
      });
      const saved = await model.call('library.publish', {
        ...(target ? { id: target.id } : {}),
        expected_revision: target?.revision ?? 0,
        name: name.trim(),
        description,
        kind,
        workflow: result.workflow,
      });
      published = saved.template;
      model.acceptSavedTemplate(saved.template);
      if (!target) {
        const linked = await model.call('library.link', {
          id: saved.template.id,
          version: saved.template.version,
          workflow: snapshot,
          ...(kind === 'workflow' ? {} : { step_ids: selected }),
          ...(overlaps.length ? { replace_sources: true } : {}),
        });
        if (!(await onLinked(linked.workflow, snapshot))) {
          error = t('factory.library.publishedUnlinked');
          return;
        }
      }
      onClose();
    } catch (cause) {
      error = failureText(cause);
    } finally {
      busy = false;
    }
  }
</script>

<Modal
  title={t('factory.library.saveTemplate')}
  size="lg"
  closeDisabled={busy}
  {onClose}
>
  {#snippet body()}<div class="modal-body factory-form">
      <div class="factory-field">
        <label for="factory-publish-kind">{t('factory.library.kind')}</label
        ><Dropdown
          id="factory-publish-kind"
          ariaLabel={t('factory.library.kind')}
          value={kind}
          options={kinds}
          disabled={busy || Boolean(published)}
          onValueChange={(value) => {
            kind = value;
            targetId = '';
            sourceId = '';
          }}
        />
      </div>
      {#if kind !== 'workflow'}<fieldset>
          <legend>{t('factory.library.stepsToSave')}</legend>
          <p class="factory-help">{t('factory.library.consecutiveHelp')}</p>
          {#each snapshot.steps as step (step.id)}<Checkbox
              id={`factory-publish-step-${step.id}`}
              checked={selected.includes(step.id)}
              disabled={busy || Boolean(published)}
              onChange={(checked) => toggle(step.id, checked)}
              >{step.name}</Checkbox
            >{/each}
        </fieldset>{/if}
      <div class="factory-field">
        <label for="factory-publish-target"
          >{t('factory.library.destination')}</label
        ><Dropdown
          id="factory-publish-target"
          ariaLabel={t('factory.library.destination')}
          value={targetId}
          disabled={busy || Boolean(published)}
          options={[
            { value: '', label: t('factory.library.new') },
            ...targets.map((item) => ({
              value: item.id,
              label: item.name,
              secondaryLabel: t('factory.library.newVersion'),
            })),
          ]}
          onValueChange={(value) => {
            targetId = value;
            sourceId = '';
            const target = targets.find((entry) => entry.id === value);
            if (target) {
              name = target.name;
              description = target.description;
            }
          }}
        />
      </div>
      {#if sources.length > 1}<div class="factory-field">
          <label for="factory-publish-source"
            >{t('factory.library.sourceUse')}</label
          >
          <Dropdown
            id="factory-publish-source"
            ariaLabel={t('factory.library.sourceUse')}
            value={sourceId}
            disabled={busy || Boolean(published)}
            options={[
              { value: '', label: t('factory.library.selectSource') },
              ...sources.map((entry, index) => ({
                value: entry.id,
                label: t('factory.library.sourceNumber', { number: index + 1 }),
                secondaryLabel: snapshot.steps
                  .filter((step) =>
                    Object.values(entry.step_map).includes(step.id),
                  )
                  .map((step) => step.name)
                  .join(', '),
              })),
            ]}
            onValueChange={(value) => (sourceId = value)}
          />
        </div>{/if}
      <div class="factory-field">
        <label for="factory-publish-name">{t('factory.library.name')}</label
        ><TextField
          id="factory-publish-name"
          maxlength="80"
          value={name}
          disabled={busy || Boolean(published)}
          onInput={(value) => (name = value)}
        />
      </div>
      <div class="factory-field">
        <label for="factory-publish-description"
          >{t('factory.workflowDescription')}</label
        ><TextArea
          id="factory-publish-description"
          maxlength="2000"
          rows={2}
          value={description}
          disabled={busy || Boolean(published)}
          onInput={(value) => (description = value)}
        />
      </div>
      <p class="factory-help">
        {targetId
          ? t('factory.library.publishExistingHelp')
          : t('factory.library.publishLinkedHelp')}
      </p>
      {#if !targetId && overlaps.length}<Banner variant="warn">
          {partialOverlap
            ? t('factory.library.partialReplacement')
            : t('factory.library.replaceLinks')}
        </Banner>{/if}
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
    </div>{/snippet}
  {#snippet footer()}<Button
      variant="secondary"
      disabled={busy}
      onClick={onClose}>{t('common.cancel')}</Button
    ><Button
      variant="primary"
      loading={busy}
      disabled={busy || Boolean(published)}
      onClick={publish}>{t('factory.library.publish')}</Button
    >{/snippet}
</Modal>
