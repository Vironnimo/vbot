<script>
  import Button from './ui/Button.svelte';
  import { floatingHoverCard } from '$lib/tooltip.js';
  import { t } from '$lib/i18n.js';

  let {
    queuedMessages = [],
    onRemoveQueuedMessage,
    onSteerQueuedMessage,
    canSteer = false,
    onEditQueuedMessage,
  } = $props();

  let steeringIds = $state([]);
  const steer = async (id) => {
    if (steeringIds.includes(id)) return;
    steeringIds = [...steeringIds, id];
    try {
      await onSteerQueuedMessage?.(id);
    } finally {
      steeringIds = steeringIds.filter((value) => value !== id);
    }
  };

  let editingId = $state('');
  let editedContent = $state('');
  let editError = $state('');
  let editSaving = $state(false);

  const beginEdit = (message) => {
    if (editSaving || message?.editable !== true) {
      return;
    }
    editingId = message.id;
    editedContent = message.content ?? '';
    editError = '';
  };

  const cancelEdit = () => {
    editingId = '';
    editedContent = '';
    editError = '';
    editSaving = false;
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

    if (editSaving) {
      return;
    }

    editSaving = true;
    let saved;
    try {
      saved =
        (await onEditQueuedMessage?.(currentEditingId, nextContent)) === true;
    } catch {
      saved = false;
    }

    if (editingId !== currentEditingId) {
      return;
    }
    editSaving = false;
    if (!saved) {
      editError = t('queue.editError', 'Queued message could not be edited.');
      return;
    }
    if (editedContent.trim() === nextContent) {
      cancelEdit();
    }
  };
</script>

{#if queuedMessages.length > 0}
  <aside
    class="queued-messages"
    aria-label={t('queue.title', 'Queued messages')}
  >
    <ol>
      {#each queuedMessages as message (message.id)}
        <li class:editing={editingId === message.id}>
          {#if editingId === message.id}
            <textarea
              aria-label={t('queue.editMessage', 'Edit queued message')}
              class="queued-messages__editor"
              value={editedContent}
              oninput={(event) => {
                editedContent = event.currentTarget.value;
                editError = '';
              }}></textarea>
            <div class="queued-messages__actions">
              <Button
                variant="tertiary"
                disabled={editSaving}
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
            <div class="queued-messages__preview">
              <button type="button" class="queued-messages__content"
                >{message.content}</button
              >
              <div class="queued-messages__full" use:floatingHoverCard>
                {message.content}
              </div>
            </div>
            <div class="queued-messages__actions">
              {#if message.steerable === true}
                <Button
                  variant="tertiary"
                  disabled={!canSteer ||
                    message.steering ||
                    steeringIds.includes(message.id)}
                  ariaLabel={t('queue.steer', 'Steer')}
                  tooltip={t(
                    'queue.steerHint',
                    'Send to the active Run at its next iteration.',
                  )}
                  onClick={() => steer(message.id)}
                >
                  {message.steering
                    ? t('queue.steering', 'Steering…')
                    : t('queue.steer', 'Steer')}
                </Button>
              {/if}
              {#if message.editable === true && !message.steering}
                <Button
                  variant="tertiary"
                  disabled={editSaving}
                  ariaLabel={t('queue.editMessage', 'Edit queued message')}
                  onClick={() => beginEdit(message)}
                >
                  <svg
                    width="13"
                    height="13"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.5"
                    aria-hidden="true"
                    ><path
                      d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15z"
                    /></svg
                  >
                </Button>
              {/if}
              <Button
                variant="tertiary"
                ariaLabel={t('queue.removeMessage', 'Remove queued message')}
                onClick={() => onRemoveQueuedMessage?.(message.id)}
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.5"
                  aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg
                >
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
    flex-shrink: 0;
    padding: 4px 12px;
    background: var(--composer-surface);
    border-radius: var(--r-md);
  }
  .queued-messages ol {
    display: flex;
    flex-direction: column;
    gap: 1px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .queued-messages li {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding: 2px 0;
  }
  .queued-messages li.editing {
    flex-wrap: wrap;
    padding: 6px 0;
  }
  .queued-messages__preview {
    flex: 1;
    min-width: 0;
    outline-offset: 2px;
  }
  .queued-messages__content {
    display: block;
    width: 100%;
    text-align: left;
    border: 0;
    padding: 0;
    background: transparent;
    font-family: inherit;
    overflow: hidden;
    color: var(--text-med);
    font-size: 12.5px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .queued-messages__full {
    position: fixed;
    z-index: var(--z-floating);
    visibility: hidden;
    pointer-events: none;
    box-shadow: var(--dropdown-elevation);
    max-width: min(560px, calc(100vw - 24px));
    max-height: min(60vh, 480px);
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    padding: 12px;
    color: var(--text-hi);
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    font-size: 13px;
    line-height: 1.5;
  }
  .queued-messages__full:global([data-floating-open='true']) {
    visibility: visible;
    pointer-events: auto;
  }
  .queued-messages__actions {
    display: flex;
    align-items: center;
    flex-shrink: 0;
    gap: 2px;
  }
  .editing .queued-messages__actions {
    margin-left: auto;
  }
  .queued-messages__actions :global(button) {
    min-height: 26px;
    padding: 3px 6px;
    font-size: 11.5px;
  }
  .queued-messages__editor {
    width: 100%;
    min-height: 68px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
    color: var(--text-hi);
    font: inherit;
    padding: 8px 10px;
    resize: vertical;
  }
  .queued-messages__editor:focus {
    border-color: var(--accent-40);
    outline: none;
    box-shadow: var(--focus-ring);
  }
  .queued-messages__error {
    margin: 0;
    color: var(--red);
    font-size: 12px;
  }
  @media (pointer: coarse) {
    .queued-messages__actions :global(button) {
      min-height: 40px;
      min-width: 40px;
    }
  }
</style>
