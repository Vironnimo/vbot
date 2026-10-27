<script>
  import Button from './ui/Button.svelte';
  import CopyButton from './ui/CopyButton.svelte';
  import Banner from './ui/Banner.svelte';
  import { tick, untrack } from 'svelte';
  import { floatingHoverCard } from '$lib/tooltip.js';
  import { t } from '$lib/i18n.js';
  import { renameAgentInKey, renameAgentInKeys } from '$lib/chatState.js';

  let {
    queuedMessages = [],
    scopeKey = '',
    subscribeAgentRenames = null,
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

  // Why Steer is unavailable for a message right now; '' when it is not.
  const steerDisabledReason = (message) => {
    if (message.steering) {
      return t('queue.steeringLocked');
    }
    if (steeringIds.includes(message.id)) {
      return t('queue.steering');
    }
    return canSteer ? '' : t('queue.steerUnavailable');
  };

  const newDraft = () => ({
    id: '',
    content: '',
    original: '',
    error: '',
    notice: '',
    saving: false,
  });
  let drafts = Object.create(null);
  let activeScope;
  let editDraft = $state(newDraft());
  $effect(() =>
    subscribeAgentRenames?.((oldId, newId) => {
      drafts = renameAgentInKeys(drafts, oldId, newId);
      activeScope = renameAgentInKey(activeScope, oldId, newId);
    }),
  );
  $effect(() => {
    const scope = scopeKey;
    if (scope === activeScope) return;
    untrack(() => {
      if (activeScope !== undefined) drafts[activeScope] = editDraft;
      editDraft = drafts[scope] ?? newDraft();
      activeScope = scope;
    });
  });
  let editorElement = $state(null);

  // Edits save only through an explicit Save, so opening another item must not
  // silently drop a modified draft: the open editor keeps focus until it is
  // saved or cancelled. An unmodified editor simply moves to the new item.
  const hasUnsavedDraft = () =>
    editDraft.id !== '' && editDraft.content !== editDraft.original;

  // Admission and reconciliation may remove the item while its editor is
  // open. Keep that editor visible until the user copies or discards the draft.
  let editUnavailable = $derived(
    editDraft.id !== '' &&
      !queuedMessages.some(
        (message) => message.id === editDraft.id && !message.steering,
      ),
  );
  let displayedMessages = $derived(
    editDraft.id &&
      !queuedMessages.some((message) => message.id === editDraft.id)
      ? [...queuedMessages, { id: editDraft.id }]
      : queuedMessages,
  );

  const focusEditor = async () => {
    const draft = editDraft;
    await tick();
    if (editDraft === draft) editorElement?.focus();
  };

  const beginEdit = (message) => {
    if (editDraft.saving || message?.editable !== true) {
      return;
    }
    if (message.id === editDraft.id) {
      focusEditor();
      return;
    }
    if (hasUnsavedDraft()) {
      editDraft.notice = t('queue.editPending');
      focusEditor();
      return;
    }
    editDraft.id = message.id;
    editDraft.content = message.content ?? '';
    editDraft.original = editDraft.content;
    editDraft.error = '';
    editDraft.notice = '';
    focusEditor();
  };

  const cancelEdit = (draft = editDraft) => {
    Object.assign(draft, newDraft());
  };

  const saveEdit = async () => {
    const draft = editDraft;
    const currentEditingId = draft.id;
    const nextContent = editDraft.content.trim();
    if (!currentEditingId || editUnavailable) {
      return;
    }

    if (!nextContent) {
      editDraft.error = t('queue.editError');
      return;
    }

    if (editDraft.saving) {
      return;
    }

    editDraft.saving = true;
    let saved;
    try {
      saved =
        (await onEditQueuedMessage?.(currentEditingId, nextContent)) === true;
    } catch {
      saved = false;
    }

    if (draft.id !== currentEditingId) {
      return;
    }
    draft.saving = false;
    if (!saved) {
      draft.error = t('queue.editError');
      return;
    }
    if (draft.content.trim() === nextContent) {
      cancelEdit(draft);
    }
  };
</script>

{#if displayedMessages.length > 0}
  <Banner
    appearance="compact"
    class="queued-messages"
    role="complementary"
    aria-label={t('queue.title')}
  >
    <ol>
      {#each displayedMessages as message (message.id)}
        <li class:editing={editDraft.id === message.id}>
          {#if editDraft.id === message.id}
            <textarea
              aria-label={t('queue.editMessage')}
              class="queued-messages__editor"
              bind:this={editorElement}
              value={editDraft.content}
              oninput={(event) => {
                editDraft.content = event.currentTarget.value;
                editDraft.error = '';
              }}></textarea>
            <div class="queued-messages__actions">
              {#if editUnavailable}
                <CopyButton text={editDraft.content} />
              {:else}
                <Button
                  variant="tertiary"
                  disabled={editDraft.saving}
                  ariaLabel={t('queue.saveEdit')}
                  onClick={saveEdit}
                >
                  {t('queue.saveEdit')}
                </Button>
              {/if}
              <Button
                variant="tertiary"
                ariaLabel={t('queue.cancelEdit')}
                onClick={() => cancelEdit()}
              >
                {t('queue.cancelEdit')}
              </Button>
              {#if !editUnavailable}
                <Button
                  variant="tertiary"
                  ariaLabel={t('queue.removeMessage')}
                  onClick={() => {
                    onRemoveQueuedMessage?.(message.id);
                    cancelEdit();
                  }}
                >
                  {t('common.remove')}
                </Button>
              {/if}
            </div>
            {#if editUnavailable}
              <p class="queued-messages__notice" role="status">
                {t('queue.editUnavailable')}
              </p>
            {:else if editDraft.error}
              <p class="queued-messages__error">{editDraft.error}</p>
            {:else if editDraft.notice}
              <p class="queued-messages__notice" role="status">
                {editDraft.notice}
              </p>
            {/if}
          {:else}
            <div class="queued-messages__preview">
              <!-- The preview opens the editor for editable items; it stays a
                   focusable control either way so keyboard users reach the
                   full-text card. -->
              <button
                type="button"
                class="queued-messages__content"
                class:queued-messages__content--editable={message.editable ===
                  true && !message.steering}
                onclick={() => {
                  if (!message.steering) beginEdit(message);
                }}>{message.content}</button
              >
              <div
                class="floating-card queued-messages__full"
                use:floatingHoverCard={{ whenTruncated: true }}
              >
                {message.content}
              </div>
            </div>
            <div class="queued-messages__actions">
              {#if message.steerable === true}
                <Button
                  variant="tertiary"
                  disabled={Boolean(steerDisabledReason(message))}
                  disabledReason={steerDisabledReason(message)}
                  ariaLabel={t('queue.steer')}
                  tooltip={t('queue.steerHint')}
                  onClick={() => steer(message.id)}
                >
                  {message.steering ? t('queue.steering') : t('queue.steer')}
                </Button>
              {/if}
              {#if message.editable === true && !message.steering}
                <Button
                  variant="tertiary"
                  disabled={editDraft.saving}
                  ariaLabel={t('queue.editMessage')}
                  tooltip={t('queue.editMessage')}
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
                ariaLabel={t('queue.removeMessage')}
                tooltip={t('queue.removeMessage')}
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
  </Banner>
{/if}

<style>
  :global(.queued-messages) {
    flex-shrink: 0;
  }
  ol {
    display: flex;
    flex-direction: column;
    gap: 1px;
    width: 100%;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  li {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding: 2px 0;
  }
  li.editing {
    flex-wrap: wrap;
    padding: 6px 0;
  }
  .queued-messages__preview {
    flex: 1;
    min-width: 0;
    outline-offset: 2px;
  }
  .queued-messages__content--editable {
    cursor: text;
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
    font-size: var(--fs-body-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* The full queued text uses the shared floating card
     (styles/app/hints.css) with a wider reading measure. */
  .queued-messages__full {
    max-width: min(560px, calc(100vw - 16px));
    white-space: pre-wrap;
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
    border-color: var(--accent);
    outline: none;
    box-shadow: var(--field-focus-ring);
  }
  .queued-messages__error,
  .queued-messages__notice {
    margin: 0;
    font-size: var(--fs-label-sm);
  }
  .queued-messages__error {
    color: var(--red);
  }
  .queued-messages__notice {
    color: var(--text-med);
  }
</style>
