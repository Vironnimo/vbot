<script>
  import { t } from '$lib/i18n.js';

  let { queuedMessages = [], onRemoveQueuedMessage } = $props();
</script>

<aside class="queued-messages" aria-label={t('queue.title', 'Queued messages')}>
  <div class="queued-messages__header">
    <h3>{t('queue.title', 'Queued messages')}</h3>
    <span
      >{t('queue.count', '{count} queued', {
        count: queuedMessages.length,
      })}</span
    >
  </div>
  {#if queuedMessages.length === 0}
    <p>{t('queue.empty', 'No queued messages.')}</p>
  {:else}
    <p>{t('queue.pending', 'Waiting for the active run to finish.')}</p>
    <ol>
      {#each queuedMessages as message (message.id)}
        <li>
          <span>{message.content}</span>
          <button
            type="button"
            onclick={() => onRemoveQueuedMessage?.(message.id)}
          >
            {t('common.remove', 'Remove')}
          </button>
        </li>
      {/each}
    </ol>
  {/if}
</aside>

<style>
  .queued-messages {
    display: grid;
    gap: var(--space-sm);
    padding: var(--space-md);
    border-top: 1px solid var(--color-border);
    background: rgba(21, 19, 15, 0.52);
  }

  .queued-messages__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-md);
  }

  .queued-messages h3,
  .queued-messages p {
    margin: 0;
  }

  .queued-messages h3 {
    color: var(--color-text);
    font-size: 1rem;
  }

  .queued-messages span,
  .queued-messages p {
    color: var(--color-muted);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-size: 0.9rem;
  }

  .queued-messages ol {
    display: grid;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .queued-messages li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: var(--space-sm);
    border: 1px solid rgba(240, 164, 58, 0.14);
    border-radius: var(--radius-md);
  }

  .queued-messages button {
    border: 0;
    color: var(--color-accent-strong);
    background: transparent;
    cursor: pointer;
  }
</style>
