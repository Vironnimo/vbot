<script>
  import { t } from '$lib/i18n.js';

  let { queuedMessages = [], onRemoveQueuedMessage } = $props();
</script>

{#if queuedMessages.length > 0}
  <aside
    class="queued-messages"
    aria-label={t('queue.title', 'Queued messages')}
  >
    <div class="queued-messages__header">
      <div>
        <h3>{t('queue.title', 'Queued messages')}</h3>
        <p>{t('queue.pending', 'Waiting for the active run to finish.')}</p>
      </div>
      <span class="chip chip-amber">
        {t('queue.count', '{count} queued', { count: queuedMessages.length })}
      </span>
    </div>
    <ol>
      {#each queuedMessages as message (message.id)}
        <li>
          <span class="queued-messages__content">{message.content}</span>
          <button
            type="button"
            class="tl-btn"
            aria-label={t('queue.removeMessage', 'Remove queued message')}
            onclick={() => onRemoveQueuedMessage?.(message.id)}
          >
            {t('common.remove', 'Remove')}
          </button>
        </li>
      {/each}
    </ol>
  </aside>
{/if}

<style>
  .queued-messages {
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex-shrink: 0;
    padding: 12px 20px;
    border-top: 1px solid var(--border);
    background: var(--surface);
  }

  .queued-messages__header,
  .queued-messages li {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
  }

  .queued-messages h3,
  .queued-messages p {
    margin: 0;
  }

  .queued-messages h3 {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 10.5px;
    font-weight: 500;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .queued-messages p {
    margin-top: 3px;
    color: var(--text-lo);
    font-size: 12px;
  }

  .queued-messages ol {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .queued-messages li {
    padding: 7px 10px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--bg);
  }

  .queued-messages__content {
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    font-size: 12.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .tl-btn {
    flex-shrink: 0;
  }

  @media (max-width: 760px) {
    .queued-messages {
      padding: 12px 14px;
    }

    .queued-messages__header,
    .queued-messages li {
      align-items: flex-start;
      flex-direction: column;
    }

    .queued-messages li {
      gap: 10px;
    }

    .queued-messages__content {
      width: 100%;
      white-space: normal;
    }
  }
</style>
