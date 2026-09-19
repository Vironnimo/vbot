<script>
  import { t } from '$lib/i18n.js';
  import Button from '../ui/Button.svelte';
  import CopyButton from '../ui/CopyButton.svelte';
  import Modal from '../ui/Modal.svelte';
  import TextArea from '../ui/TextArea.svelte';

  let {
    state: inputState,
    label = t('jev.evaluatedState', 'Evaluated state'),
  } = $props();
  let open = $state(false);
  let json = $derived(typeof inputState !== 'string');
  let text = $derived(
    json ? (JSON.stringify(inputState, null, 2) ?? '') : inputState,
  );
  // Bound the excerpt as well as its layout; large states mount only on request.
  let excerpt = $derived(text.slice(0, 400).replace(/\s+/g, ' ').trim());
</script>

<section class="jev-state-preview" aria-label={label}>
  <div class="jev-row">
    <strong>{label}</strong>
    <span class="jev-help"
      >{json ? 'JSON' : t('jev.text', 'Text')} · {t(
        'jev.characterCount',
        '{count} characters',
        { count: text.length.toLocaleString() },
      )}</span
    >
  </div>
  <p class="jev-state-excerpt">
    {excerpt || t('jev.emptyState', 'Empty state')}{text.length > 400
      ? '…'
      : ''}
  </p>
  <Button variant="tertiary" onClick={() => (open = true)}
    >{t('jev.viewState', 'View full state')}</Button
  >
</section>

{#if open}
  <Modal title={label} class="jev-state-dialog" onClose={() => (open = false)}>
    {#snippet body()}
      <div class="jev-state-reader">
        <p class="jev-help">
          {t(
            'jev.stateSnapshotHelp',
            'The exact state used for this decision. Later setup edits do not change it.',
          )}
        </p>
        <TextArea
          class="jev-state-full"
          ariaLabel={label}
          value={text}
          readonly
          code={json}
          rows={18}
        />
      </div>
    {/snippet}
    {#snippet footer()}
      <CopyButton {text} label={t('jev.copyState', 'Copy full state')} />
      <Button onClick={() => (open = false)}
        >{t('common.close', 'Close')}</Button
      >
    {/snippet}
  </Modal>
{/if}
