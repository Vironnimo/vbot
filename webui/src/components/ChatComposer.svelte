<script>
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';
  import FileAutocomplete from './FileAutocomplete.svelte';
  import ModelAutocomplete from './ModelAutocomplete.svelte';
  import SkillAutocomplete from './SkillAutocomplete.svelte';
  import Button from './ui/Button.svelte';
  import ContextMenu from './ui/ContextMenu.svelte';
  import { contextMenuAnchor } from './ui/contextMenu.js';
  import { t } from '$lib/i18n.js';
  import { onDestroy, tick, untrack } from 'svelte';
  import {
    flushComposerMemory,
    getDraft,
    getHistory,
    setDraft,
  } from '$lib/composerMemory.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import ContextUsageRing from './chat/ContextUsageRing.svelte';
  import { createComposerMedia } from './composer/media.svelte.js';
  import { createComposerPicker } from './composer/picker.svelte.js';
  import { createComposerSubmission } from './composer/submission.svelte.js';
  import ComposerWidgetStates from './composer/ComposerWidgetStates.svelte';
  import './composer/composer.css';

  let {
    disabled = false,
    isRunning = false,
    cancelling = false,
    availableSkills = [],
    contextUsage = null,
    compactionState = 'unavailable',
    compactionSubmitting = false,
    onForceCompaction = null,
    contextWindow = null,
    // The displayed Session's effective Compaction Policy (null while unknown).
    compactionPolicy = null,
    usage = null,
    sessionUsage = null,
    draftKey = '',
    historyKey = '',
    focusRequest = 0,
    // Resolves true when the message was accepted, false otherwise. A send
    // that created the Session of an unsaved draft resolves `{ draftKey }`,
    // the key the composer continues in.
    onSendMessage,
    onCancelRun = () => {},
    // Known work of this Session that outlives its Run (a running Sub-Agent
    // or background command, or a waiting Session): Stop all stays offered
    // after the Run ended.
    backgroundWorkRunning = false,
    stoppingAll = false,
    onStopAll = () => {},
    onTranscriptionError,
    onListFiles = null,
    onLoadModelCatalog = null,
    // Opens the Secret form; a bare `/secret` calls it instead of sending.
    onOpenSecretForm = null,
    computerControl,
    // A row under the input box (the Session settings pickers).
    footer,
  } = $props();
  const media = createComposerMedia({
    get draftKey() {
      return draftKey;
    },
    get onTranscriptionError() {
      return onTranscriptionError;
    },
    get disabled() {
      return disabled;
    },
    get insertTranscript() {
      return insertTranscript;
    },
  });
  const picker = createComposerPicker({
    get availableSkills() {
      return availableSkills;
    },
    get content() {
      return content;
    },
    set content(value) {
      content = value;
    },
    get inputElement() {
      return inputElement;
    },
    get onListFiles() {
      return onListFiles;
    },
    get onLoadModelCatalog() {
      return onLoadModelCatalog;
    },
    get resizeInput() {
      return resizeInput;
    },
    get noteContentEdited() {
      return noteContentEdited;
    },
    get executeImmediateCommand() {
      return executeImmediateCommand;
    },
  });
  let content = $state('');
  // Input-history navigation. `historyCursor` is -1 while editing the live draft
  // (the "bottom" slot) and 0..n-1 when a sent message is recalled (newest
  // first). `navWorkingCopies` keeps a per-slot working copy for the duration of
  // one navigation session, so editing a recalled message and arrowing away then
  // back restores the edit instead of discarding it (readline-style).
  let historyCursor = -1;
  let navWorkingCopies = {};
  let lastDraftKey = null;
  let handledFocusRequest = 0;
  let inputElement = $state(null);

  let inputOrigin = $state('');
  const submission = createComposerSubmission({
    media,
    picker,
    get disabled() {
      return disabled;
    },
    get content() {
      return content;
    },
    set content(value) {
      content = value;
    },
    get inputOrigin() {
      return inputOrigin;
    },
    set inputOrigin(value) {
      inputOrigin = value;
    },
    get draftKey() {
      return draftKey;
    },
    get historyKey() {
      return historyKey;
    },
    get onListFiles() {
      return onListFiles;
    },
    get onSendMessage() {
      return onSendMessage;
    },
    get onOpenSecretForm() {
      return onOpenSecretForm;
    },
    resetInputHistory() {
      historyCursor = -1;
      navWorkingCopies = {};
      resetInputHeight();
    },
    refreshMovedDraft(toKey) {
      if (
        draftKey === toKey &&
        lastDraftKey === toKey &&
        content !== getDraft(toKey)
      ) {
        content = getDraft(toKey);
        tick().then(resizeInput);
      }
    },
  });

  // While a Run is active, Stop cancels it and its menu offers Stop all; once
  // only background work runs, Stop all stands alone. The menu closes with
  // the Run, since it renders inside the Run's Stop control.
  let stopMenu = $state(null);

  function openStopMenu(event) {
    stopMenu = {
      ...contextMenuAnchor(event),
      label: t('chat.stopOptions'),
      items: [
        {
          id: 'stop-run',
          label: t('chat.cancelRun'),
          group: 'run',
          disabled: cancelling,
          hint: cancelling ? t('cancel.cancelling') : '',
          onSelect: onCancelRun,
        },
        {
          id: 'stop-all',
          label: t('chat.stopAll'),
          group: 'all',
          danger: true,
          disabled: stoppingAll,
          onSelect: onStopAll,
        },
      ],
    };
  }

  // Why Send is unavailable right now. A disabled composer (no Agent, History
  // loading) explains itself, so it gives no reason here.
  let sendDisabledReason = $derived(
    disabled
      ? ''
      : submission.inFlight
        ? t('chat.sendUnavailableSending')
        : media.hasUploadingAttachments
          ? t('chat.sendUnavailableUploading')
          : media.voiceBusy || media.isRecording
            ? t('chat.sendUnavailableVoice')
            : !content.trim() && media.pendingAttachments.length === 0
              ? t('chat.sendUnavailableEmpty')
              : '',
  );

  // Move this area's draft to another Agent's draft (the footer's Agent
  // picker): its text and attachments follow, after what that draft holds.
  export function moveDraft(fromKey, toKey) {
    submission.moveDraft(fromKey, toKey, { merge: true });
  }

  onDestroy(() => {
    media.destroy();
    // Leaving the Chat tab tears this component down; make sure the latest
    // debounced draft reaches durable storage before it goes.
    flushComposerMemory();
  });

  // Load the draft for the displayed session whenever the session changes (and
  // on first mount). The outgoing session's draft is already persisted on each
  // edit, so switching never loses it — we only swap in the incoming one and
  // reset history navigation to its draft slot.
  $effect(() => {
    const key = draftKey;
    if (key === lastDraftKey) {
      return;
    }
    lastDraftKey = key;
    media.cancelActiveRecording();
    media.hydratePendingAttachments(key);
    historyCursor = -1;
    navWorkingCopies = {};
    inputOrigin = '';
    picker.resetForDraft();
    content = getDraft(key);
    tick().then(() => {
      if (content) {
        resizeInput();
      } else {
        resetInputHeight();
      }
    });
  });

  // A new file listing (another draft Project) replaces the cached list.
  let lastListFiles = untrack(() => onListFiles);
  $effect(() => {
    const listFiles = onListFiles;
    if (listFiles === lastListFiles) {
      return;
    }
    lastListFiles = listFiles;
    untrack(() => picker.resetFileCandidates());
  });

  // ChatView issues focus requests only for deliberate user navigation. Keep
  // the DOM detail here so a request made while history is loading waits until
  // the textarea is enabled, and never lets focus scroll the timeline.
  $effect(() => {
    const request = focusRequest;
    if (!request || request === handledFocusRequest || disabled) {
      return;
    }
    tick().then(() => {
      if (focusRequest === request && !disabled && inputElement) {
        inputElement.focus({ preventScroll: true });
        handledFocusRequest = request;
      }
    });
  });

  // A reload fires `beforeunload` while this component is still mounted; flush so
  // an in-progress draft survives it. The listener is removed on unmount.
  $effect(() => {
    if (typeof window === 'undefined') {
      return;
    }
    const handleBeforeUnload = () => flushComposerMemory();
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  });

  const insertTranscript = async (transcript) => {
    const text = typeof transcript === 'string' ? transcript.trim() : '';
    if (!text) {
      return;
    }
    content = content.trim() ? `${content.trimEnd()}\n${text}` : text;
    inputOrigin = 'speech_transcription';
    noteContentEdited();
    picker.triggerContext = null;
    picker.activeSkillIndex = 0;
    await tick();
    inputElement?.focus();
    resizeInput();
  };

  const focusInputFromWrap = (event) => {
    if (event.target === inputElement) {
      return;
    }

    if (event.target?.closest?.('button, input, a')) {
      return;
    }

    event.preventDefault();
    inputElement?.focus();
  };

  const focusInputFromWrapAction = (node) => {
    node.addEventListener('mousedown', focusInputFromWrap);

    return {
      destroy() {
        node.removeEventListener('mousedown', focusInputFromWrap);
      },
    };
  };

  const resizeInput = () => {
    if (!inputElement) {
      return;
    }
    inputElement.style.height = 'auto';
    inputElement.style.height = `${inputElement.scrollHeight}px`;
  };

  const resetInputHeight = () => {
    if (!inputElement) {
      return;
    }

    inputElement.style.height = '';
    inputElement.scrollTop = 0;
  };

  // Record the current text into the active navigation slot. While editing the
  // live draft (cursor -1) that also persists it; edits to a recalled history
  // entry stay a transient working copy that never overwrites the draft.
  const noteContentEdited = () => {
    navWorkingCopies[historyCursor] = content;
    if (historyCursor === -1) {
      setDraft(draftKey, content);
    }
  };

  // History starts beyond the text's ends, never within a logical line that
  // may wrap onto several visible lines. A selection belongs to native editing.
  const caretAt = (position) =>
    inputElement?.selectionStart === position &&
    inputElement?.selectionEnd === position;

  const applyNavSlot = (history) => {
    const slotText =
      historyCursor in navWorkingCopies
        ? navWorkingCopies[historyCursor]
        : historyCursor === -1
          ? getDraft(draftKey)
          : (history[historyCursor] ?? '');
    content = slotText;
    picker.triggerContext = null;
    picker.activeSkillIndex = 0;
    // Don't auto-open the skill popup from recalled text that begins with `/`.
    picker._triggerClosed = true;
    if (historyCursor === -1) {
      setDraft(draftKey, content);
    }
    tick().then(() => {
      if (!inputElement) {
        return;
      }
      const caret = content.length;
      inputElement.setSelectionRange(caret, caret);
      if (content) {
        resizeInput();
      } else {
        resetInputHeight();
      }
    });
  };

  // Returns true when the key was consumed (caller prevents the default caret
  // move). Going older stashes the slot we leave so nothing is lost on return.
  const recallOlderMessage = () => {
    const history = getHistory(historyKey);
    if (history.length === 0) {
      return false;
    }
    navWorkingCopies[historyCursor] = content;
    if (historyCursor >= history.length - 1) {
      // Already at the oldest entry: hold position, but still swallow the key.
      return true;
    }
    historyCursor += 1;
    applyNavSlot(history);
    return true;
  };

  const recallNewerMessage = () => {
    const history = getHistory(historyKey);
    navWorkingCopies[historyCursor] = content;
    historyCursor -= 1;
    applyNavSlot(history);
    return true;
  };

  // The skill/command, file, and model popups share one keyboard contract;
  // these pick the popup that is currently open.

  const handleKeydown = (event) => {
    // An active IME composition owns Enter, arrows and Escape (candidate
    // navigation/confirmation); none of them may submit, recall or close.
    if (isImeComposing(event)) {
      return;
    }
    const autocompleteOpen =
      picker.showSkillAutocomplete ||
      picker.showFileAutocomplete ||
      picker.showModelAutocomplete;
    if (autocompleteOpen) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        picker._suppressSelectionUpdate = true;
        picker.moveActiveIndex(
          Math.min(picker.activeSkillIndex + 1, picker.activeMatchCount() - 1),
        );
        return;
      }

      if (event.key === 'ArrowUp') {
        event.preventDefault();
        picker._suppressSelectionUpdate = true;
        picker.moveActiveIndex(Math.max(picker.activeSkillIndex - 1, 0));
        return;
      }

      if (event.key === 'Tab') {
        if (picker.activeAutocompleteElement()?.selectActive()) {
          event.preventDefault();
        }
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        picker._triggerClosed = true;
        picker.triggerContext = null;
        picker.activeSkillIndex = 0;
        return;
      }
    }

    // Input history — only when a popup isn't already using the arrow keys.
    // Plain Up at the start recalls older messages; plain Down at the end
    // returns toward the live draft. Modified arrows retain native navigation.
    if (
      !autocompleteOpen &&
      !event.defaultPrevented &&
      !event.shiftKey &&
      !event.ctrlKey &&
      !event.altKey &&
      !event.metaKey
    ) {
      if (event.key === 'ArrowUp' && caretAt(0) && recallOlderMessage()) {
        event.preventDefault();
        return;
      }
      if (
        event.key === 'ArrowDown' &&
        historyCursor !== -1 &&
        caretAt(content.length) &&
        recallNewerMessage()
      ) {
        event.preventDefault();
        return;
      }
    }

    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }

    if (autocompleteOpen) {
      if (picker.activeAutocompleteElement()?.selectActive()) {
        event.preventDefault();
        return;
      }
      // A picker still loading its catalog has no row to choose yet; Enter
      // waits for it instead of sending the partial @/model token as text.
      if (picker.activeAutocompleteLoading()) {
        event.preventDefault();
        return;
      }
    }

    event.preventDefault();
    submission.submit();
  };

  const handleInput = () => {
    picker._triggerClosed = false;
    if (!content.trim()) {
      inputOrigin = '';
    }
    noteContentEdited();
    resizeInput();
    picker.updateTriggerContext();
  };

  const handleSelection = () => {
    if (picker._suppressSelectionUpdate) {
      picker._suppressSelectionUpdate = false;
      return;
    }

    picker.updateTriggerContext();
  };

  // A no-argument built-in command runs the instant it is chosen from the `/`
  // popup — no second Enter is needed. Replace the partial token with the
  // canonical command before using the regular guarded submit path so failures
  // leave a retryable draft and successful admission clears it normally.
  const executeImmediateCommand = (skill) => {
    const normalizedName = String(skill.name).replace(/^\/+/, '');
    if (!normalizedName) {
      return;
    }
    const command = `/${normalizedName}`;
    submission.submitCommand(command);
  };

  // A model chosen from the /model argument popup is submitted immediately —
  // no second Enter is needed. The option's canonical value (including any
  // connection/account suffix) becomes the /model argument through the regular
  // guarded submit path so failures leave a retryable draft.
  const selectModel = (option) => {
    if (!picker.triggerContext || !option?.value) {
      return;
    }

    const command = `/model ${option.value}`;
    submission.submitCommand(command);
  };
</script>

<form
  class="input-area"
  class:drag-over={media.isDragOver}
  aria-label={t('chat.composerLabel')}
  ondragover={media.handleDragOver}
  ondragleave={media.handleDragLeave}
  ondrop={media.handleDrop}
  onsubmit={(event) => {
    event.preventDefault();
    submission.submit();
  }}
>
  <input
    bind:this={media.fileInputElement}
    class="attachment-file-input"
    type="file"
    accept={media.ATTACHMENT_ACCEPT}
    multiple
    {disabled}
    onchange={media.handleFilePickerChange}
  />
  {#if media.attachmentToastMessage}
    <div class="composer-toast" role="status" aria-live="polite">
      <p class="composer-toast-title">{t('errors.appError')}</p>
      <p class="composer-toast-message">{media.attachmentToastMessage}</p>
    </div>
  {/if}
  {#if picker.showSkillAutocomplete}
    <SkillAutocomplete
      bind:this={picker.autocompleteElement}
      skills={picker.autocompleteItems}
      query={picker.autocompleteQuery}
      marker={picker.triggerContext.marker}
      activeIndex={picker.activeSkillIndex}
      onSelect={picker.selectSkill}
      onHover={picker.moveActiveIndex}
    />
  {/if}
  {#if picker.showFileAutocomplete}
    <FileAutocomplete
      bind:this={picker.fileAutocompleteElement}
      candidates={picker.fileRows}
      truncated={picker.fileRowsTruncated}
      loading={picker.fileRowsLoading}
      activeIndex={picker.activeSkillIndex}
      onSelect={picker.selectFile}
      onHover={picker.moveActiveIndex}
    />
  {/if}
  {#if picker.showModelAutocomplete}
    <ModelAutocomplete
      bind:this={picker.modelAutocompleteElement}
      options={picker.modelOptions}
      query={picker.autocompleteQuery}
      loading={picker.modelCatalogLoading}
      footerLabel={picker.modelFilterFooter}
      onFooterAction={() => (picker.showAllModels = !picker.showAllModels)}
      activeIndex={picker.activeSkillIndex}
      onSelect={selectModel}
      onHover={picker.moveActiveIndex}
    />
  {/if}
  <!-- Speech progress shows in the placeholder and on the microphone; this
       region only announces it. -->
  <div class="composer-voice-status" role="status" aria-atomic="true">
    {media.voiceBusy ? media.voiceStatus : ''}
  </div>
  <div
    class="input-wrap"
    role="group"
    aria-label={t('chat.composerArea')}
    use:focusInputFromWrapAction
  >
    <div class="msg-input-field">
      {#if picker.commandArgumentHint}
        <!-- What may follow the typed command, greyed behind the caret. -->
        <div class="msg-input-ghost" aria-hidden="true">
          <span class="msg-input-ghost__typed"
            >{picker.commandArgumentHint.typed}</span
          ><span class="msg-input-ghost__hint"
            >{picker.commandArgumentHint.hint}</span
          >
        </div>
      {/if}
      <textarea
        bind:this={inputElement}
        bind:value={content}
        class="msg-input"
        {disabled}
        aria-label={t('chat.composerLabel')}
        oninput={handleInput}
        onkeydown={handleKeydown}
        onpaste={media.handlePaste}
        onclick={handleSelection}
        onkeyup={handleSelection}
        placeholder={media.voiceBusy
          ? media.voiceStatus
          : t('chat.composerPlaceholder')}
        rows="1"></textarea>
    </div>
    <ContextUsageRing
      {contextUsage}
      {contextWindow}
      {usage}
      {sessionUsage}
      {compactionPolicy}
      {onForceCompaction}
      {compactionState}
      {compactionSubmitting}
      {disabled}
    />
    <div class="input-btns">
      <span class="tooltip-anchor" use:tooltip={media.microphoneLabel}>
        <Button
          variant="tertiary"
          icon
          class={media.isRecording ? 'btn-icon--active' : ''}
          disabled={disabled || media.voiceBusy}
          loading={media.voiceBusy}
          ariaLabel={media.microphoneLabel}
          onClick={media.handleMicrophoneClick}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="M8 2a2 2 0 0 1 2 2v4a2 2 0 1 1-4 0V4a2 2 0 0 1 2-2z" />
            <path d="M4 7v1a4 4 0 0 0 8 0V7M8 12v2M6 14h4" />
          </svg>
        </Button>
      </span>
      <Button
        variant="tertiary"
        icon
        {disabled}
        ariaLabel={t('chat.attachment.addFile')}
        tooltip={t('chat.attachment.addFile')}
        onClick={media.handleFilePickerClick}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
          />
        </svg>
      </Button>
      {@render computerControl?.()}
      {#if isRunning}
        <!-- Run-level cancel lives next to Send: while a run is active both
             actions coexist — Send queues, the stop button cancels. It is
             deliberately independent of the composer `disabled` state so a
             run stays cancellable even while the input is locked. Its menu
             button offers Stop all, which also ends the Session's background
             commands, terminals and Sub-Agents. -->
        <span class="composer-stop composer-stop-group">
          <Button
            variant="danger"
            icon
            class="composer-stop-run"
            disabled={cancelling}
            disabledReason={cancelling ? t('cancel.cancelling') : ''}
            ariaLabel={cancelling
              ? t('cancel.cancelling')
              : t('chat.cancelRun')}
            tooltip={{
              title: t('chat.cancelRun'),
              text: t('chat.cancelRunHint'),
            }}
            onClick={onCancelRun}
          >
            <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
              <rect
                x="3.5"
                y="3.5"
                width="7"
                height="7"
                rx="1"
                fill="currentColor"
                stroke="none"
              />
            </svg>
          </Button>
          <Button
            variant="danger"
            icon
            class="composer-stop-menu"
            loading={stoppingAll}
            ariaLabel={t('chat.stopOptions')}
            tooltip={{
              title: t('chat.stopOptions'),
              text: t('chat.stopOptionsHint'),
            }}
            aria-haspopup="menu"
            aria-expanded={stopMenu !== null}
            onClick={openStopMenu}
          >
            <svg viewBox="0 0 10 14" width="9" height="13" aria-hidden="true">
              <path d="M2 5.5 5 8.5l3-3" />
            </svg>
          </Button>
        </span>
        <ContextMenu menu={stopMenu} onClose={() => (stopMenu = null)} />
      {:else if backgroundWorkRunning}
        <Button
          variant="danger"
          icon
          class="composer-stop composer-stop-all"
          loading={stoppingAll}
          ariaLabel={t('chat.stopAll')}
          tooltip={{ title: t('chat.stopAll'), text: t('chat.stopAllHint') }}
          onClick={onStopAll}
        >
          <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
            <rect
              x="1.5"
              y="1.5"
              width="7"
              height="7"
              rx="1"
              fill="currentColor"
              stroke="none"
              opacity="0.5"
            />
            <rect
              x="5.5"
              y="5.5"
              width="7"
              height="7"
              rx="1"
              fill="currentColor"
              stroke="none"
            />
          </svg>
        </Button>
      {/if}
      <Button
        type="submit"
        variant="primary"
        icon
        disabled={disabled || Boolean(sendDisabledReason)}
        disabledReason={sendDisabledReason}
        ariaLabel={isRunning ? t('chat.queueMessage') : t('chat.sendMessage')}
        tooltip={isRunning
          ? { title: t('chat.queueMessage'), text: t('chat.queueMessageHint') }
          : { title: t('chat.sendMessage'), text: t('chat.sendMessageHint') }}
      >
        <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
          <path d="M12 7L2 2l2 5-2 5 10-5z" fill="currentColor" stroke="none" />
        </svg>
      </Button>
    </div>
  </div>
  {#if media.pendingAttachments.length > 0}
    <div class="attachment-tray" aria-label={t('chat.attachment.preview')}>
      {#each media.pendingAttachments as attachment, index (attachment.local_id)}
        <div
          class="attachment-item"
          class:attachment-item-image={media.hasImageMediaType(
            attachment.media_type,
          )}
        >
          {#if media.hasImageMediaType(attachment.media_type)}
            <button
              type="button"
              class="attachment-thumb-trigger"
              aria-label={t('chat.attachment.preview')}
            >
              <img
                src={attachment.preview_url}
                alt={attachment.filename}
                class="attachment-thumb"
              />
              <span
                class="floating-card attachment-hover-preview"
                aria-hidden="true"
                use:floatingHoverCard={{ accessible: false }}
              >
                <img
                  src={attachment.preview_url}
                  alt=""
                  class="attachment-hover-image"
                />
              </span>
            </button>
          {:else}
            <span class="attachment-file-icon" aria-hidden="true">
              <svg viewBox="0 0 16 16">
                <path
                  d="M4 1h5l3 3v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1zm4 1v2h2"
                />
              </svg>
            </span>
          {/if}
          <div class="attachment-meta">
            <span
              class="attachment-name"
              use:tooltip={{ text: attachment.filename, whenTruncated: true }}
              >{attachment.filename}</span
            >
            {#if attachment.uploading}
              <span class="attachment-status">
                {t('chat.attachment.uploading')}
              </span>
            {:else if !media.hasImageMediaType(attachment.media_type)}
              <span class="attachment-status">
                {t('chat.attachment.fileLabel')}
              </span>
            {/if}
          </div>
          <button
            type="button"
            class="attachment-remove"
            aria-label={t('chat.attachment.removeNamed', {
              name: attachment.filename,
            })}
            use:tooltip={t('chat.attachment.remove')}
            onclick={() => media._removeAttachment(index)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </div>
      {/each}
    </div>
  {/if}
  <ComposerWidgetStates sessionKey={draftKey} />
  {#if footer}
    <div class="composer-footer">
      {@render footer()}
    </div>
  {/if}
</form>
