<script>
  import Button from './ui/Button.svelte';
  import StatusChip from './ui/StatusChip.svelte';
  import { t } from '$lib/i18n.js';

  let {
    queuedMessages = [],
    onRemoveQueuedMessage,
    onEditQueuedMessage,
  } = $props();

  let editingId = $state('');
  let editedContent = $state('');
  let editError = $state('');

  const beginEdit = (message) => {
    editingId = message.id;
    editedContent = message.content ?? '';
    editError = '';
  };

  const cancelEdit = () => {
    editingId = '';
    editedContent = '';
    editError = '';
  };

  const saveEdit = async () => {
    const currentEditingId = editingId;
    const nextContent = editedContent.trim();
    if (!currentEditingId) {
      return;
    }

    if (!nextContent) {
      editError = t('queue.editError', 'Queued message could not be edited.');
      return;
    }

    await onEditQueuedMessage?.(currentEditingId, nextContent);
    cancelEdit();
  };
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
      <StatusChip variant="warn">
        {t('queue.count', '{count} queued', { count: queuedMessages.length })}
      </StatusChip>
    </div>
    <ol>
      {#each queuedMessages as message (message.id)}
        <li>
          {#if editingId === message.id}
            <textarea
              class="queued-messages__editor"
              value={editedContent}
              oninput={(event) => {
                editedContent = event.currentTarget.value;
                editError = '';
              }}
            ></textarea>
            <div class="queued-messages__actions">
              <Button
                variant="tertiary"
                ariaLabel={t('queue.saveEdit', 'Save edit')}
                onClick={saveEdit}
              >
                {t('queue.saveEdit', 'Save')}
              </Button>
              <Button
                variant="tertiary"
                ariaLabel={t('queue.cancelEdit', 'Cancel edit')}
                onClick={cancelEdit}
              >
                {t('queue.cancelEdit', 'Cancel')}
              </Button>
              <Button
                variant="tertiary"
                ariaLabel={t('queue.removeMessage', 'Remove queued message')}
                onClick={() => {
                  onRemoveQueuedMessage?.(message.id);
                  cancelEdit();
                }}
              >
                {t('common.remove', 'Remove')}
              </Button>
            </div>
            {#if editError}
              <p class="queued-messages__error">{editError}</p>
            {/if}
          {:else}
            <span class="queued-messages__content">{message.content}</span>
            <div class="queued-messages__actions">
              <Button
                variant="tertiary"
                ariaLabel={t('queue.editMessage', 'Edit queued message')}
                onClick={() => beginEdit(message)}
              >
                {t('queue.editMessage', 'Edit')}
              </Button>
              <Button
                variant="tertiary"
                ariaLabel={t('queue.removeMessage', 'Remove queued message')}
                onClick={() => onRemoveQueuedMessage?.(message.id)}
              >
                {t('common.remove', 'Remove')}
              </Button>
            </div>
          {/if}
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
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }

  .queued-messages__content {
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    font-size: 12.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
    width: 100%;
  }

  .queued-messages__actions {
    display: flex;
    width: 100%;
    justify-content: flex-end;
    gap: 8px;
  }

  .queued-messages__editor {
    width: 100%;
    min-height: 68px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 12.5px;
    line-height: 1.4;
    padding: 8px 10px;
    resize: vertical;
  }

  .queued-messages__editor:focus {
    border-color: rgba(232, 135, 10, 0.4);
    outline: none;
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
  }

  .queued-messages__error {
    margin: 0;
    color: var(--red);
    font-size: 12px;
  }

  .queued-messages__actions :global(.btn-tertiary) {
    flex-shrink: 0;
  }

  @media (max-width: 640px) {
    .queued-messages {
      padding: 12px 14px;
    }

    .queued-messages__header,
    .queued-messages li {
      align-items: flex-start;
      flex-direction: column;
    }

    .queued-messages__actions {
      justify-content: flex-start;
    }

    .queued-messages__content {
      width: 100%;
      white-space: normal;
    }
  }
</style>
