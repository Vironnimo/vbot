<script>
  // The Memory section of expanded Tool details: the entries a call added,
  // removed or reworded in one Memory scope, and the Memory history revision
  // that recorded them. Changes come from `toolDetailBlocks`.
  import { t } from '$lib/i18n.js';

  let { block } = $props();

  const SCOPE_LABELS = {
    agent: () => t('chat.memoryScope.agent'),
    user: () => t('chat.memoryScope.user'),
  };
  const CHANGE_LABELS = {
    added: () => t('chat.memoryChange.added'),
    removed: () => t('chat.memoryChange.removed'),
  };

  // A reworded entry reads as its old text removed and its new text added.
  let rows = $derived(
    block.changes.flatMap((change) =>
      change.op === 'replaced'
        ? [
            { kind: 'removed', text: change.previous },
            { kind: 'added', text: change.text },
          ]
        : [{ kind: change.op, text: change.text }],
    ),
  );
</script>

<div class="teb-row teb-section tool-memory">
  <div class="teb-section-header">
    <span class="teb-label">{SCOPE_LABELS[block.scope]()}</span>
    {#if block.revision}
      <span class="tool-memory__revision"
        >{t('chat.memoryRevision', { revision: block.revision })}</span
      >
    {/if}
  </div>
  <ul class="tool-memory__changes">
    {#each rows as row, index (index)}
      <li class={`tool-memory__change tool-memory__change--${row.kind}`}>
        <span
          class="tool-memory__marker"
          role="img"
          aria-label={CHANGE_LABELS[row.kind]()}
          >{row.kind === 'added' ? '+' : '-'}</span
        >
        <span class="tool-memory__text">{row.text}</span>
      </li>
    {/each}
  </ul>
</div>
