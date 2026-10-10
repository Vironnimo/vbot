<script>
  // The Wiki page editor: title and Markdown source with the save state. An
  // existing page autosaves through WikiPanel's participant; a new page is
  // created only by the Save action. WikiPanel owns the draft and its saves.
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import FormField from '../../../../webui/src/components/ui/FormField.svelte';
  import SaveStatus from '../../../../webui/src/components/ui/SaveStatus.svelte';
  import TextArea from '../../../../webui/src/components/ui/TextArea.svelte';
  import TextField from '../../../../webui/src/components/ui/TextField.svelte';

  let {
    page,
    title,
    content,
    titleError,
    saving,
    pending,
    onTitle,
    onContent,
    onDone,
    onCancel,
    onSave,
  } = $props();

  let root = $state(null);

  /** Moves keyboard focus to the title field. */
  export function focusTitle() {
    root?.querySelector('#wiki-title')?.focus();
  }

  /** Moves keyboard focus to the start of the Markdown source. */
  export function focusContent() {
    const field = root?.querySelector('#wiki-content');
    if (!field) return;
    // Place the caret first: focusing scrolls the field to its caret, which a
    // freshly set value puts at the end.
    field.setSelectionRange(0, 0);
    field.focus();
    field.scrollTop = 0;
  }

  const reference = $derived(page?.number != null ? `w${page.number}` : '');
</script>

<div class="wiki-editor" bind:this={root}>
  <h3 class="wiki-editor__heading">
    {#if page}{#if reference}<span class="wiki-number">{reference}</span
        >{/if}<span>{t('swarm.wiki.editPage')}</span>{:else}{t(
        'swarm.wiki.new',
      )}{/if}
  </h3>
  <FormField
    controlId="wiki-title"
    label={t('swarm.wiki.pageTitle')}
    error={titleError}
  >
    {#snippet children(field)}
      <TextField
        id={field.controlId}
        invalid={field.invalid}
        aria-describedby={field.describedBy}
        value={title}
        onInput={(value) => onTitle(value)}
      />
    {/snippet}
  </FormField>
  <FormField
    controlId="wiki-content"
    label={t('swarm.wiki.content')}
    help={t('swarm.wiki.contentHelp')}
  >
    {#snippet children(field)}
      <TextArea
        id={field.controlId}
        aria-describedby={field.describedBy}
        code
        spellcheck="false"
        rows={18}
        value={content}
        onInput={(value) => onContent(value)}
      />
    {/snippet}
  </FormField>
  <div class="wiki-editor__footer">
    {#if page}<Button variant="tertiary" onClick={onDone}
        >{t('swarm.wiki.preview')}</Button
      >{:else}<Button variant="tertiary" onClick={onCancel}
        >{t('common.cancel')}</Button
      >{/if}
    <SaveStatus {saving} {pending} onClick={onSave} />
  </div>
</div>

<style>
  .wiki-editor {
    display: grid;
    gap: var(--space-md);
    min-width: 0;
  }
  .wiki-editor__heading {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin: 0 0 var(--space-xs);
    color: var(--text-hi);
    font: 600 var(--fs-heading-lg) / 1.3 var(--font-ui);
  }
  .wiki-number {
    color: var(--text-lo);
    font: 600 var(--fs-label-md) / 1.4 var(--font-ui);
    font-variant-numeric: tabular-nums;
  }
  /* Markdown source reads in the code face but wraps like prose, so long
     paragraphs need no sideways scrolling. */
  .wiki-editor :global(#wiki-content) {
    min-height: 320px;
    white-space: pre-wrap;
    overflow-wrap: break-word;
  }
  .wiki-editor__footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
  }
</style>
