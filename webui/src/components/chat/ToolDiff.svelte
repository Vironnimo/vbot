<script>
  // The Changes section of expanded Tool details: each file the Tool changed
  // with its kind of change, line counts and a numbered diff in a scroll box
  // of bounded height. Rows come from `toolFileChanges`.
  import { fileChangesCopyText } from '$lib/chatToolDetails.js';
  import { boundedScroll } from '$lib/boundedScroll.js';
  import { t } from '$lib/i18n.js';
  import CopyButton from '../ui/CopyButton.svelte';

  let { changes } = $props();

  const CHANGE_LABELS = {
    created: () => t('chat.fileChange.created'),
    updated: () => t('chat.fileChange.updated'),
    replaced: () => t('chat.fileChange.replaced'),
    deleted: () => t('chat.fileChange.deleted'),
    moved: () => t('chat.fileChange.moved'),
  };
  const MARKERS = { added: '+', removed: '-', context: ' ' };

  function gutterWidth(rows) {
    const widest = rows.reduce(
      (width, row) => Math.max(width, String(row.number ?? '').length),
      1,
    );
    return `${widest}ch`;
  }
</script>

<div class="teb-row teb-section tool-diff">
  <div class="teb-section-header">
    <span class="teb-label">{t('chat.toolChanges')}</span>
    <CopyButton
      text={fileChangesCopyText(changes)}
      class="chat-copy-action tool-detail-copy"
      label={t('chat.copyToolField', { label: t('chat.toolChanges') })}
    />
  </div>
  {#each changes as change, index (`${index}:${change.path}`)}
    <div class="tool-diff-file">
      <div class="tool-diff-file__header">
        <span class="tool-diff-file__kind"
          >{CHANGE_LABELS[change.change]()}</span
        >
        <span class="tool-diff-file__path"
          >{change.destination
            ? `${change.path} → ${change.destination}`
            : change.path}</span
        >
        {#if change.added > 0 || change.removed > 0}
          <span class="te-fact te-fact--added">+{change.added}</span>
          <span class="te-fact te-fact--removed">-{change.removed}</span>
        {/if}
      </div>
      {#if change.binary}
        <p class="tool-diff-file__note">{t('chat.fileChange.binary')}</p>
      {:else if change.rows.length > 0}
        <div
          class="teb-scroll tool-diff-file__body"
          role="group"
          aria-label={change.path}
          use:boundedScroll
        >
          <div
            class="tool-diff-lines"
            style:--diff-gutter={gutterWidth(change.rows)}
          >
            {#each change.rows as row, rowIndex (rowIndex)}
              {#if row.kind === 'gap'}
                <div
                  class="tool-diff-line tool-diff-line--gap"
                  aria-hidden="true"
                >
                  <span class="tool-diff-line__number"></span>
                  <span class="tool-diff-line__marker"></span>
                  <span class="tool-diff-line__text">⋯</span>
                </div>
              {:else}
                <div class={`tool-diff-line tool-diff-line--${row.kind}`}>
                  <span class="tool-diff-line__number">{row.number}</span>
                  <span class="tool-diff-line__marker">{MARKERS[row.kind]}</span
                  >
                  <span class="tool-diff-line__text">{row.text}</span>
                </div>
              {/if}
            {/each}
          </div>
        </div>
      {/if}
      {#if change.omittedLines > 0}
        <p class="tool-diff-file__note">
          {t('chat.fileChange.omitted', { count: change.omittedLines })}
        </p>
      {/if}
    </div>
  {/each}
</div>
