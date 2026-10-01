<script>
  // "Restore as": restores an Agent, Project or single-Session entry under a
  // new id, for example when its own id is taken again. Opens with the
  // conflict that made it necessary; the entries panel sends the restore and
  // passes back its refusal.
  import Banner from '../ui/Banner.svelte';
  import Button from '../ui/Button.svelte';
  import FormField from '../ui/FormField.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextField from '../ui/TextField.svelte';
  import { restoreAsFieldLabel } from '$lib/archiveView.js';
  import { t } from '$lib/i18n.js';

  const noop = () => {};

  let {
    entry,
    conflictText = '',
    error = '',
    busy = false,
    onSubmit = noop,
    onClose = noop,
  } = $props();

  let targetId = $state('');
  let trimmed = $derived(targetId.trim());

  function submit(event) {
    event.preventDefault();
    if (!trimmed || busy) return;
    onSubmit(trimmed);
  }
</script>

<Modal
  title={t('archive.restoreAs.title')}
  labelledById="archive-restore-as-title"
  closeDisabled={busy}
  {onClose}
>
  {#snippet body()}
    <form onsubmit={submit}>
      <div class="modal-body archive-restore-as">
        {#if conflictText}
          <Banner variant="warn">{conflictText}</Banner>
        {/if}
        <p class="archive-restore-as__hint">
          {t('archive.restoreAs.hint', { name: entry.label })}
        </p>
        <FormField
          controlId="archive-restore-as-id"
          label={restoreAsFieldLabel(entry.kind)}
          {error}
        >
          <TextField
            id="archive-restore-as-id"
            variant="modal"
            code
            value={targetId}
            placeholder={entry.subject_id}
            invalid={Boolean(error)}
            disabled={busy}
            autocomplete="off"
            spellcheck="false"
            onInput={(next) => (targetId = next)}
          />
        </FormField>
      </div>
      <div class="modal-footer">
        <Button variant="secondary" disabled={busy} onClick={onClose}>
          {t('common.cancel')}
        </Button>
        <Button
          variant="primary"
          type="submit"
          loading={busy}
          disabled={busy || !trimmed}
        >
          {t('archive.action.restore')}
        </Button>
      </div>
    </form>
  {/snippet}
</Modal>

<style>
  .archive-restore-as {
    display: grid;
    gap: 14px;
  }

  .archive-restore-as__hint {
    margin: 0;
    color: var(--text-med);
    line-height: 1.5;
  }
</style>
