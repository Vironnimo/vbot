<script>
  import { untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import Dropdown from '../../../../webui/src/components/Dropdown.svelte';
  import Modal from '../../../../webui/src/components/ui/Modal.svelte';
  import { copy, failureText } from './presentation.js';
  import { templateKind } from './library.js';

  let { model, workflow, onInsert, onClose } = $props();
  const snapshot = untrack(() => copy(workflow));
  let id = $state('');
  let after = $state('end');
  let busy = $state(false);
  let error = $state('');
  const templates = $derived(model.library.filter((item) => !item.archived));
  const item = $derived(templates.find((entry) => entry.id === id));
  const places = [
    { value: 'start', label: t('factory.library.atStart') },
    ...snapshot.steps.map((step) => ({
      value: step.id,
      label: t('factory.library.afterStep', { name: step.name }),
    })),
    { value: 'end', label: t('factory.library.atEnd') },
  ];

  async function insert() {
    if (!item || busy) return;
    busy = true;
    try {
      const result = await model.call('library.insert', {
        id: item.id,
        version: item.latest_version ?? item.version,
        workflow: snapshot,
        ...(after === 'end' ? {} : { after: after === 'start' ? [] : [after] }),
      });
      if (await onInsert(result.workflow, snapshot)) onClose();
      else error = t('factory.library.draftChanged');
    } catch (cause) {
      error = failureText(cause);
    } finally {
      busy = false;
    }
  }
</script>

<Modal title={t('factory.library.insert')} closeDisabled={busy} {onClose}>
  {#snippet body()}<div class="modal-body factory-form">
      <div class="factory-field">
        <label for="factory-library-insert-template"
          >{t('factory.library.template')}</label
        ><Dropdown
          id="factory-library-insert-template"
          ariaLabel={t('factory.library.template')}
          disabled={busy}
          value={id}
          options={templates.map((entry) => ({
            value: entry.id,
            label: entry.name,
            secondaryLabel: `${templateKind(entry.kind)} · ${t('factory.library.version', { version: entry.latest_version ?? entry.version })}`,
          }))}
          onValueChange={(value) => (id = value)}
        />
      </div>
      {#if item?.description}<p>{item.description}</p>{/if}
      <div class="factory-field">
        <label for="factory-library-insert-after"
          >{t('factory.library.position')}</label
        ><Dropdown
          id="factory-library-insert-after"
          ariaLabel={t('factory.library.position')}
          disabled={busy}
          value={after}
          options={places}
          onValueChange={(value) => (after = value)}
        />
      </div>
      <p class="factory-help">{t('factory.library.linkedHelp')}</p>
      {#if error}<Banner variant="error" role="alert">{error}</Banner>{/if}
    </div>{/snippet}
  {#snippet footer()}<Button
      variant="secondary"
      disabled={busy}
      onClick={onClose}>{t('common.cancel')}</Button
    ><Button variant="primary" loading={busy} disabled={!item} onClick={insert}
      >{t('factory.library.insert')}</Button
    >{/snippet}
</Modal>
