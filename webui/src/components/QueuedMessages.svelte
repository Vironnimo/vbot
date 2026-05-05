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
      <span class="queued-messages__dot" aria-hidden="true"></span>
      <h3>{t('queue.title', 'Queued messages')}</h3>
      <span class="queued-messages__count">
        {t('queue.count', '{count} queued', {
          count: queuedMessages.length,
        })}
      </span>
    </div>
    <p>{t('queue.pending', 'Waiting for the active run to finish.')}</p>
    <ol>
      {#each queuedMessages as message (message.id)}
        <li>
          <span>{message.content}</span>
          <button
            type="button"
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
    display: grid;
    flex-shrink: 0;
    gap: var(--space-sm);
    padding: 10px var(--space-lg);
    border-top: 1px solid var(--border);
    background: var(--bg);
  }

  .queued-messages__header {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .queued-messages__dot {
    width: 6px;
    height: 6px;
    flex-shrink: 0;
    border-radius: 50%;
    animation: queue-blink 1.2s ease-in-out infinite;
    background: var(--amber);
  }

  .queued-messages h3,
  .queued-messages p {
    margin: 0;
  }

  .queued-messages h3 {
    color: var(--text-med);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }

  .queued-messages__count,
  .queued-messages p,
  .queued-messages li span {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .queued-messages ol {
    display: flex;
    gap: var(--space-sm);
    margin: 0;
    padding: 0;
    overflow-x: auto;
    list-style: none;
  }

  .queued-messages li {
    display: flex;
    min-width: 15rem;
    max-width: 22rem;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-sm);
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    background: var(--surface);
  }

  .queued-messages li span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .queued-messages button {
    flex-shrink: 0;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    padding: 2px 8px;
    background: transparent;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
    transition:
      border-color 120ms ease,
      color 120ms ease;
  }

  .queued-messages button:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  @keyframes queue-blink {
    0%,
    100% {
      opacity: 1;
    }

    50% {
      opacity: 0.35;
    }
  }
</style>
