<script>
  import { t } from '$lib/i18n.js';
  import {
    compactionSeparatorLabel,
    compactionSummaryText,
  } from '$lib/chatTimelinePresentation.js';

  import CopyButton from '../ui/CopyButton.svelte';

  let { item, inRun = false } = $props();

  const summaryText = $derived(compactionSummaryText(item));
  const running = $derived(item?.status === 'running');
</script>

{#if running || !summaryText}
  <div
    class="date-sep compaction-sep"
    class:run-compaction-sep={inRun}
    class:compaction-sep--running={running}
    role={running ? 'status' : undefined}
    aria-busy={running || undefined}
  >
    {compactionSeparatorLabel(item)}
  </div>
{:else}
  <details
    class="compaction-disclosure"
    class:compaction-disclosure--in-run={inRun}
  >
    <summary class="date-sep compaction-sep" class:run-compaction-sep={inRun}>
      <span class="compaction-sep__trigger">
        <span>{compactionSeparatorLabel(item)}</span>
        <svg
          class="compaction-sep__chevron"
          viewBox="0 0 16 16"
          width="10"
          height="10"
          aria-hidden="true"
        >
          <path d="M4 6l4 4 4-4" />
        </svg>
      </span>
    </summary>
    <div class="compaction-detail">
      <div class="compaction-detail__header">
        <span>{t('chat.compactionContext', 'Compaction context')}</span>
        <CopyButton
          text={summaryText}
          class="chat-copy-action compaction-copy"
          label={t('chat.copyCompaction', 'Copy compaction context')}
          copiedLabel={t('chat.compactionCopied', 'Compaction context copied')}
        />
      </div>
      <pre class="compaction-detail__text">{summaryText}</pre>
    </div>
  </details>
{/if}
