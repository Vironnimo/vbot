import { SvelteMap } from 'svelte/reactivity';
import { t } from '$lib/i18n.js';
import { scheduleAutosave } from '$lib/autosave.js';
import {
  updatePromptBlock,
  resetPromptBlock,
  setPromptLayout,
  createPromptBlock,
  removePromptBlock,
  resetPromptLayout,
} from '$lib/api.js';
import { tick } from 'svelte';

export function createPromptEditor(context) {
  const AUTO_SAVE_DEBOUNCE_MS = 800;

  const MAX_PROMPT_FLUSH_PASSES = 10;

  // The custom-block slug rule mirrors the backend agent-id rule (validated again
  // at the RPC edge and the store): letters/digits plus `-`/`_`, alphanumeric
  // start, bounded length. This is a UX pre-check; the server stays authoritative.
  const SLUG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/u;

  // Blocks come from `prompt.list` in layout order. Each block is keyed by its
  // stable `id` (never an array index), so autosave timers and row identity
  // survive a reorder. Editable text blocks carry `editedContent`/`isDirty`
  // live-edit state; data blocks (`kind === 'data'`) have none.
  let blocks = $state([]);

  // Autosave timers keyed by block id (a reorder must not reassign a timer to a
  // different block, which an index key would do). A plain null-proto object,
  // not reactive state — it only holds setTimeout handles.
  const autoSaveTimers = Object.create(null);

  const blockSavePromises = new SvelteMap();

  let hasUnsavedEdits = $derived(
    blocks.some((block) => block.editable && block.isDirty),
  );

  const promptAutosaveParticipant = {
    flush: flushPendingPromptAutosaves,
    hasPending: () =>
      blockSavePromises.size > 0 ||
      Object.keys(autoSaveTimers).length > 0 ||
      hasUnsavedEdits,
  };

  const unregisterPromptAutosave = context.autosaveContext.register(
    promptAutosaveParticipant,
  );

  // Pending confirmations (null = the dialog is closed). Each destructive action
  // opens its own dialog and runs only once the user confirms. `resetBlock` and
  // `removeCustomBlock` remember the target block id; `resetLayout` takes none.
  let resetConfirmBlockId = $state(null);

  let removeConfirmBlockId = $state(null);

  let resetLayoutConfirmOpen = $state(false);

  // The reset-block confirm body speaks of the Default scope's built-in default
  // or an Agent scope's inherited Default content, matching the scope in effect.
  let resetConfirmBody = $derived(
    context.isAgentScope
      ? t('systemPrompt.fragmentEditor.resetAgentConfirm')
      : t('systemPrompt.fragmentEditor.resetConfirm'),
  );

  let isBusy = $derived(blocks.some((block) => block.isSaving || block.isBusy));

  // -- Owner / inheritance labels ------------------------------------------
  // The owner is gate 2 of the three-gate prompt filter: a block renders only
  // while its owner condition holds. This turns the internal owner token into a
  // plain sentence explaining that render condition.
  function ownerHint(owner) {
    if (owner.startsWith('tool:')) {
      return t('systemPrompt.blockList.ownerHint.tool', {
        name: owner.slice('tool:'.length),
      });
    }
    if (owner.startsWith('extension:')) {
      return t('systemPrompt.blockList.ownerHint.extension', {
        name: owner.slice('extension:'.length),
      });
    }
    if (owner === 'memory') {
      return t('systemPrompt.blockList.ownerHint.memory');
    }
    if (owner === 'channel') {
      return t('systemPrompt.blockList.ownerHint.channel');
    }
    return t('systemPrompt.blockList.ownerHint.always');
  }

  function dataKindLabel() {
    return t('systemPrompt.blockList.dataLabel');
  }

  function isCustomBlock(block) {
    return block.source === 'user';
  }

  // An inherited block shows the greyed default + "inherited" badge in an agent
  // scope (T5). Inheritance is a text-cascade concept, so it applies only to
  // editable blocks — a data block has no override to inherit or create.
  function isInherited(block) {
    return block.editable && block.inheritance === 'owner_default';
  }

  // -- Edit + autosave ------------------------------------------------------
  function blockIndexById(blockId) {
    return blocks.findIndex((block) => block.id === blockId);
  }

  function handleTextareaInput(blockId, nextContent) {
    const index = blockIndexById(blockId);
    if (index === -1) {
      return;
    }
    blocks[index].editedContent = nextContent;
    blocks[index].isDirty = nextContent !== blocks[index].content;

    clearAutoSaveTimer(blockId);
    if (blocks[index].isDirty) {
      scheduleAutoSaveTimer(blockId);
    }
  }

  function scheduleAutoSaveTimer(blockId) {
    if (autoSaveTimers[blockId]) {
      return;
    }
    autoSaveTimers[blockId] = scheduleAutosave(() => {
      delete autoSaveTimers[blockId];
      void saveBlock(blockId);
    }, AUTO_SAVE_DEBOUNCE_MS);
  }

  function clearAutoSaveTimer(blockId) {
    const timer = autoSaveTimers[blockId];
    if (timer) {
      timer();
      delete autoSaveTimers[blockId];
    }
  }

  function clearAutoSaveTimers() {
    for (const blockId of Object.keys(autoSaveTimers)) {
      autoSaveTimers[blockId]();
      delete autoSaveTimers[blockId];
    }
  }

  async function flushPendingPromptAutosaves() {
    clearAutoSaveTimers();

    for (let pass = 0; pass < MAX_PROMPT_FLUSH_PASSES; pass += 1) {
      const activeResults = await Promise.all(blockSavePromises.values());
      if (!activeResults.every(Boolean)) {
        return false;
      }

      const dirtyIds = blocks
        .filter((block) => block.editable && block.isDirty)
        .map((block) => block.id);
      if (dirtyIds.length === 0) {
        return true;
      }

      const results = await Promise.all(dirtyIds.map(saveBlock));
      if (!results.every(Boolean)) {
        return false;
      }
    }

    return false;
  }

  function saveBlock(blockId) {
    const activeSave = blockSavePromises.get(blockId);
    if (activeSave) {
      return activeSave.then((saved) => {
        if (!saved) return false;
        const block = blocks.find((entry) => entry.id === blockId);
        return block?.isDirty ? saveBlock(blockId) : true;
      });
    }

    const operation = persistBlock(blockId);
    blockSavePromises.set(blockId, operation);
    void operation.finally(() => {
      if (blockSavePromises.get(blockId) === operation) {
        blockSavePromises.delete(blockId);
      }
    });
    return operation;
  }

  async function persistBlock(blockId) {
    const index = blockIndexById(blockId);
    if (index === -1) {
      return false;
    }
    const block = blocks[index];

    if (!block.editable || !block.isDirty || block.isSaving || block.isBusy) {
      return false;
    }

    const draftContent = block.editedContent;
    const scopeKey = context.blocksScopeKey;
    blocks[index].isSaving = true;

    try {
      const result = await updatePromptBlock({
        id: block.id,
        content: draftContent,
        ...context.scopedParams(),
      });

      const liveIndex = blockIndexById(blockId);
      // Saved; rows listed for another scope meanwhile keep their own text.
      if (liveIndex === -1 || context.blocksScopeKey !== scopeKey) {
        return true;
      }
      const nextSaved =
        typeof result.text === 'string' ? result.text : draftContent;
      blocks[liveIndex].content = nextSaved;
      if (blocks[liveIndex].editedContent === draftContent) {
        blocks[liveIndex].editedContent = nextSaved;
        blocks[liveIndex].isDirty = false;
      } else {
        blocks[liveIndex].isDirty =
          blocks[liveIndex].editedContent !== blocks[liveIndex].content;
      }
      blocks[liveIndex].isModified = result.is_modified === true;
      if (typeof result.inheritance === 'string') {
        blocks[liveIndex].inheritance = result.inheritance;
      }
      context.schedulePreviewRefresh();
      return true;
    } catch {
      context.showToast(t('systemPrompt.error.saveFailed'), 'error');
      return false;
    } finally {
      const liveIndex = blockIndexById(blockId);
      if (liveIndex !== -1) {
        blocks[liveIndex].isSaving = false;
      }
    }
  }

  async function handleManualSaveAll() {
    const dirtyIds = blocks
      .filter((block) => block.editable && block.isDirty)
      .map((block) => block.id);

    for (const blockId of dirtyIds) {
      clearAutoSaveTimer(blockId);
    }

    await Promise.all(dirtyIds.map(saveBlock));
  }

  function resetBlock(blockId) {
    if (blockIndexById(blockId) === -1) {
      return;
    }
    resetConfirmBlockId = blockId;
  }

  function cancelResetBlock() {
    resetConfirmBlockId = null;
  }

  async function confirmResetBlock() {
    const blockId = resetConfirmBlockId;
    resetConfirmBlockId = null;
    const index = blockIndexById(blockId);
    if (index === -1) {
      return;
    }
    const block = blocks[index];

    clearAutoSaveTimer(blockId);
    blocks[index].isBusy = true;

    try {
      const result = await resetPromptBlock(
        context.scopedParams({ id: block.id }),
      );
      const liveIndex = blockIndexById(blockId);
      if (liveIndex === -1) {
        return;
      }
      const restored = typeof result.text === 'string' ? result.text : '';
      blocks[liveIndex].content = restored;
      blocks[liveIndex].editedContent = restored;
      blocks[liveIndex].isDirty = false;
      blocks[liveIndex].isModified = result.is_modified === true;
      if (typeof result.inheritance === 'string') {
        blocks[liveIndex].inheritance = result.inheritance;
      }
      context.schedulePreviewRefresh();
    } catch {
      context.showToast(t('systemPrompt.error.resetFailed'), 'error');
    } finally {
      const liveIndex = blockIndexById(blockId);
      if (liveIndex !== -1) {
        blocks[liveIndex].isBusy = false;
      }
    }
  }

  // -- Toggle + layout persistence -----------------------------------------
  // Build the `[{id, enabled, source}]` layout payload from the current row order
  // and send it to `prompt.set_layout`, which persists immediately (T6).
  async function persistLayout() {
    try {
      await setPromptLayout(
        context.scopedParams({
          layout: blocks.map((block) => ({
            id: block.id,
            enabled: block.enabled,
            source: block.source,
          })),
        }),
      );
      context.schedulePreviewRefresh();
    } catch {
      context.showToast(t('systemPrompt.error.layoutFailed'), 'error');
      // Re-sync from the server so the on-screen order/toggle matches what is
      // actually persisted after a failed write.
      await context.loadBlocksForScope(context.selectedScopeKey);
    }
  }

  async function toggleBlock(blockId) {
    const index = blockIndexById(blockId);
    if (index === -1) {
      return;
    }
    blocks[index].enabled = !blocks[index].enabled;
    await persistLayout();
  }

  function togglePreview(blockId) {
    const index = blockIndexById(blockId);
    if (index !== -1) {
      blocks[index].previewExpanded = !blocks[index].previewExpanded;
    }
  }

  // -- Reorder (SortableList: pointer drag and Alt+Arrow keys) ------------
  async function reorderBlocks(from, to) {
    const next = [...blocks];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    blocks = next;
    await persistLayout();
  }

  // -- Custom block create / remove (T1) -----------------------------------
  async function createCustomBlock() {
    const slug = window.prompt(t('systemPrompt.blockList.newBlockPrompt'));
    if (slug === null) {
      return;
    }
    const trimmed = slug.trim();
    if (!trimmed) {
      return;
    }
    if (!SLUG_PATTERN.test(trimmed)) {
      context.showToast(t('systemPrompt.blockList.invalidSlug'), 'error');
      return;
    }

    try {
      await createPromptBlock(context.scopedParams({ slug: trimmed }));
      await context.loadBlocksForScope(context.selectedScopeKey);
      const created = blocks.find((block) => block.id === `user:${trimmed}`);
      if (created) {
        created.editorExpanded = true;
        await tick();
        document
          .getElementById(`sp-block-body-${created.id}`)
          ?.querySelector('textarea')
          ?.focus();
      }
      context.schedulePreviewRefresh();
    } catch {
      context.showToast(t('systemPrompt.blockList.createFailed'), 'error');
    }
  }

  function removeCustomBlock(blockId) {
    removeConfirmBlockId = blockId;
  }

  function cancelRemoveCustomBlock() {
    removeConfirmBlockId = null;
  }

  async function confirmRemoveCustomBlock() {
    const blockId = removeConfirmBlockId;
    removeConfirmBlockId = null;
    if (!blockId) {
      return;
    }

    clearAutoSaveTimer(blockId);
    try {
      await removePromptBlock(context.scopedParams({ id: blockId }));
      await context.loadBlocksForScope(context.selectedScopeKey);
      context.schedulePreviewRefresh();
    } catch {
      context.showToast(t('systemPrompt.blockList.removeFailed'), 'error');
    }
  }

  function resetLayout() {
    resetLayoutConfirmOpen = true;
  }

  function cancelResetLayout() {
    resetLayoutConfirmOpen = false;
  }

  async function confirmResetLayout() {
    resetLayoutConfirmOpen = false;

    try {
      await resetPromptLayout(context.scopedParams());
      await context.loadBlocksForScope(context.selectedScopeKey);
      context.schedulePreviewRefresh();
    } catch {
      context.showToast(t('systemPrompt.error.layoutFailed'), 'error');
    }
  }
  function destroy() {
    unregisterPromptAutosave();
    clearAutoSaveTimers();
  }

  return {
    destroy,
    get blocks() {
      return blocks;
    },
    set blocks(value) {
      blocks = value;
    },
    get resetConfirmBlockId() {
      return resetConfirmBlockId;
    },
    set resetConfirmBlockId(value) {
      resetConfirmBlockId = value;
    },
    get removeConfirmBlockId() {
      return removeConfirmBlockId;
    },
    set removeConfirmBlockId(value) {
      removeConfirmBlockId = value;
    },
    get resetLayoutConfirmOpen() {
      return resetLayoutConfirmOpen;
    },
    set resetLayoutConfirmOpen(value) {
      resetLayoutConfirmOpen = value;
    },
    get resetConfirmBody() {
      return resetConfirmBody;
    },
    set resetConfirmBody(value) {
      resetConfirmBody = value;
    },
    get isBusy() {
      return isBusy;
    },
    get hasUnsavedEdits() {
      return hasUnsavedEdits;
    },
    set isBusy(value) {
      isBusy = value;
    },
    get ownerHint() {
      return ownerHint;
    },
    get dataKindLabel() {
      return dataKindLabel;
    },
    get isCustomBlock() {
      return isCustomBlock;
    },
    get isInherited() {
      return isInherited;
    },
    get handleTextareaInput() {
      return handleTextareaInput;
    },
    get clearAutoSaveTimers() {
      return clearAutoSaveTimers;
    },
    get handleManualSaveAll() {
      return handleManualSaveAll;
    },
    get resetBlock() {
      return resetBlock;
    },
    get cancelResetBlock() {
      return cancelResetBlock;
    },
    get confirmResetBlock() {
      return confirmResetBlock;
    },
    get toggleBlock() {
      return toggleBlock;
    },
    get togglePreview() {
      return togglePreview;
    },
    get reorderBlocks() {
      return reorderBlocks;
    },
    get createCustomBlock() {
      return createCustomBlock;
    },
    get removeCustomBlock() {
      return removeCustomBlock;
    },
    get cancelRemoveCustomBlock() {
      return cancelRemoveCustomBlock;
    },
    get confirmRemoveCustomBlock() {
      return confirmRemoveCustomBlock;
    },
    get resetLayout() {
      return resetLayout;
    },
    get cancelResetLayout() {
      return cancelResetLayout;
    },
    get confirmResetLayout() {
      return confirmResetLayout;
    },
  };
}
