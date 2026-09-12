<script>
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';
  import FileAutocomplete from './FileAutocomplete.svelte';
  import ModelAutocomplete from './ModelAutocomplete.svelte';
  import SkillAutocomplete from './SkillAutocomplete.svelte';
  import Button from './ui/Button.svelte';
  import { t } from '$lib/i18n.js';
  import { onDestroy, tick } from 'svelte';
  import {
    clearDraft,
    flushComposerMemory,
    getDraft,
    getHistory,
    pushHistory,
    setDraft,
  } from '$lib/composerMemory.js';
  import {
    extractMentionTokens,
    matchMentionCandidates,
  } from '$lib/fileMentions.js';
  import { formatTokenUsageTooltip } from '$lib/tokenUsageTooltip.js';
  import { createComposerMedia } from './composer/media.svelte.js';
  import { createComposerPicker } from './composer/picker.svelte.js';
  import './composer/composer.css';

  let {
    disabled = false,
    isRunning = false,
    cancelling = false,
    availableSkills = [],
    contextUsage = null,
    compactionState = 'unavailable',
    compactionSubmitting = false,
    onForceCompaction = () => {},
    contextWindow = null,
    usage = null,
    sessionUsage = null,
    draftKey = '',
    historyKey = '',
    focusRequest = 0,
    onSendMessage,
    onCancelRun = () => {},
    onTranscriptionError,
    onListFiles = null,
    onLoadModelCatalog = null,
    computerControl,
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
  let submitInFlight = $state(false);

  // Context-window fill ring: a thin SVG progress arc proportional to
  // tokens / context_window. The same tooltip as the old header badge.
  const CONTEXT_RING_RADIUS = 6;
  const CONTEXT_RING_CIRCUMFERENCE = 2 * Math.PI * CONTEXT_RING_RADIUS;

  let contextFillRatio = $derived.by(() => {
    const tokens = contextUsage?.tokens;
    if (
      !Number.isFinite(tokens) ||
      !Number.isFinite(contextWindow) ||
      contextWindow <= 0
    ) {
      return null;
    }
    return Math.min(1, tokens / contextWindow);
  });
  let contextRingOffset = $derived(
    contextFillRatio === null
      ? CONTEXT_RING_CIRCUMFERENCE
      : CONTEXT_RING_CIRCUMFERENCE * (1 - contextFillRatio),
  );
  let contextTooltip = $derived(
    formatTokenUsageTooltip(contextUsage, usage, sessionUsage, contextWindow) ??
      undefined,
  );

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

  // Which @-tokens in the outgoing text are actual files. Decided against the
  // picker's file list (fetched now if this draft never opened the picker, e.g.
  // a restored draft), so pasted code decorators and handles never expand.
  const collectFileMentions = async (snapshot) => {
    let files = snapshot.fileCandidates;
    if (files === null && typeof snapshot.listFiles === 'function') {
      try {
        const result = await snapshot.listFiles();
        files = Array.isArray(result?.files) ? result.files : [];
        if (draftKey === snapshot.draftKey) {
          picker.fileCandidates = files;
          picker.fileListTruncated = Boolean(result?.truncated);
        }
      } catch {
        files = [];
      }
    }
    return matchMentionCandidates(snapshot.mentionTokens, files ?? []);
  };

  const createSubmitSnapshot = () => ({
    content,
    trimmedContent: content.trim(),
    inputOrigin,
    draftKey,
    historyKey,
    mentionTokens: extractMentionTokens(content),
    fileCandidates:
      picker.fileCandidates === null ? null : Array.from(picker.fileCandidates),
    listFiles: onListFiles,
    sendMessage: onSendMessage,
    attachments: media.pendingAttachments.map((attachment) => ({
      source: attachment,
      attachment_id: attachment.attachment_id,
      filename: attachment.filename,
      media_type: attachment.media_type,
    })),
  });

  const submitBlocked = (snapshot) =>
    disabled ||
    submitInFlight ||
    media.hasUploadingAttachments ||
    media.voiceBusy ||
    (!snapshot.trimmedContent && snapshot.attachments.length === 0);

  const submit = async (snapshot = createSubmitSnapshot()) => {
    if (submitBlocked(snapshot)) {
      return;
    }

    submitInFlight = true;
    media.cancelActiveRecording();
    try {
      const fileMentions =
        snapshot.mentionTokens.length === 0
          ? []
          : await collectFileMentions(snapshot);
      await finishSubmit(snapshot, fileMentions);
    } finally {
      submitInFlight = false;
    }
  };

  const finishSubmit = async (snapshot, fileMentions) => {
    const sendOptionCandidates = {
      ...(snapshot.inputOrigin ? { inputOrigin: snapshot.inputOrigin } : {}),
      ...(fileMentions.length > 0 ? { fileMentions } : {}),
    };
    const sendOptions =
      Object.keys(sendOptionCandidates).length > 0
        ? sendOptionCandidates
        : null;

    let outgoingContent;
    if (snapshot.attachments.length === 0) {
      outgoingContent = snapshot.content;
    } else {
      const contentBlocks = snapshot.attachments
        .filter((attachment) => attachment.attachment_id)
        .flatMap((attachment) => {
          if (media.hasMediaMediaType(attachment.media_type)) {
            return [
              {
                type: 'media',
                attachment_id: attachment.attachment_id,
                filename: attachment.filename,
                media_type: attachment.media_type,
              },
            ];
          }

          const fileBlock = {
            type: 'file',
            attachment_id: attachment.attachment_id,
            filename: attachment.filename,
            media_type: attachment.media_type,
          };

          return [fileBlock];
        });

      if (snapshot.trimmedContent) {
        contentBlocks.unshift({
          type: 'text',
          text: snapshot.trimmedContent,
        });
      }

      if (contentBlocks.length === 0) {
        return false;
      }
      outgoingContent = contentBlocks;
    }

    if (typeof snapshot.sendMessage !== 'function') {
      return false;
    }

    let sent;
    try {
      sent =
        (sendOptions
          ? await snapshot.sendMessage(outgoingContent, sendOptions)
          : await snapshot.sendMessage(outgoingContent)) === true;
    } catch {
      sent = false;
    }
    if (!sent) {
      return false;
    }

    // Only successful admission makes this a sent-history entry. A failed RPC
    // leaves both the draft and its recall history untouched.
    pushHistory(snapshot.historyKey, snapshot.content);

    const submittedAttachmentSources = new Set(
      snapshot.attachments.map((attachment) => attachment.source),
    );
    const submittedAttachmentsStillPresent = media
      .attachmentsForScope(media.attachmentScopeForDraftKey(snapshot.draftKey))
      .filter((attachment) => submittedAttachmentSources.has(attachment));
    for (const attachment of submittedAttachmentsStillPresent) {
      media.safeRevokeObjectUrl(attachment.preview_url);
    }
    media.updateAttachmentsForDraftKey(snapshot.draftKey, (attachments) =>
      attachments.filter(
        (attachment) => !submittedAttachmentSources.has(attachment),
      ),
    );

    // Typing and navigation stay available while admission is pending. Clear
    // only the exact draft snapshot that succeeded; later edits or another
    // Session's draft must survive the older request completing.
    if (getDraft(snapshot.draftKey) === snapshot.content) {
      clearDraft(snapshot.draftKey);
    }
    if (draftKey === snapshot.draftKey && content === snapshot.content) {
      content = '';
      inputOrigin = '';
      picker.triggerContext = null;
      picker.activeSkillIndex = 0;
      media.isDragOver = false;
      historyCursor = -1;
      navWorkingCopies = {};
      resetInputHeight();
    }
    return true;
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

  // Up recalls history only when the caret sits on the first logical line, so a
  // multi-line draft can still be navigated normally before the first line
  // hands off to history ("keep going up").
  const caretOnFirstLine = () => {
    if (!inputElement) {
      return true;
    }
    const start = inputElement.selectionStart ?? 0;
    if (start !== (inputElement.selectionEnd ?? start)) {
      return false;
    }
    return !content.slice(0, start).includes('\n');
  };

  const caretOnLastLine = () => {
    if (!inputElement) {
      return true;
    }
    const end = inputElement.selectionEnd ?? content.length;
    if ((inputElement.selectionStart ?? end) !== end) {
      return false;
    }
    return !content.slice(end).includes('\n');
  };

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
    const autocompleteOpen =
      picker.showSkillAutocomplete ||
      picker.showFileAutocomplete ||
      picker.showModelAutocomplete;
    if (autocompleteOpen) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        picker._suppressSelectionUpdate = true;
        picker.activeSkillIndex = Math.min(
          picker.activeSkillIndex + 1,
          picker.activeMatchCount() - 1,
        );
        return;
      }

      if (event.key === 'ArrowUp') {
        event.preventDefault();
        picker._suppressSelectionUpdate = true;
        picker.activeSkillIndex = Math.max(picker.activeSkillIndex - 1, 0);
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
    // Up walks into older sent messages from the first line; Down walks back
    // toward (and finally into) the live draft.
    if (!autocompleteOpen) {
      if (
        event.key === 'ArrowUp' &&
        caretOnFirstLine() &&
        recallOlderMessage()
      ) {
        event.preventDefault();
        return;
      }
      if (
        event.key === 'ArrowDown' &&
        historyCursor !== -1 &&
        caretOnLastLine() &&
        recallNewerMessage()
      ) {
        event.preventDefault();
        return;
      }
    }

    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }

    if (
      autocompleteOpen &&
      picker.activeAutocompleteElement()?.selectActive()
    ) {
      event.preventDefault();
      return;
    }

    event.preventDefault();
    submit();
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

  // Mirror ModelAutocomplete's match set exactly (same predicate) so keyboard
  // navigation and the rendered list never disagree.

  // A /model argument trigger: content starts with "/model" followed by a
  // space, and the cursor sits at or after that space. The query is everything
  // after the space (extracted via the shared autocompleteQuery derived).

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
    const candidateSnapshot = {
      ...createSubmitSnapshot(),
      content: command,
      trimmedContent: command,
      inputOrigin: '',
      mentionTokens: [],
      attachments: [],
    };
    if (submitBlocked(candidateSnapshot)) {
      return;
    }

    content = command;
    setDraft(draftKey, command);
    inputOrigin = '';
    picker.triggerContext = null;
    picker.activeSkillIndex = 0;
    picker._triggerClosed = true;
    media.isDragOver = false;
    historyCursor = -1;
    navWorkingCopies = {};
    resetInputHeight();
    void submit(candidateSnapshot);
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
    const candidateSnapshot = {
      ...createSubmitSnapshot(),
      content: command,
      trimmedContent: command,
      inputOrigin: '',
      mentionTokens: [],
      attachments: [],
    };
    if (submitBlocked(candidateSnapshot)) {
      return;
    }

    content = command;
    setDraft(draftKey, command);
    inputOrigin = '';
    picker.triggerContext = null;
    picker.activeSkillIndex = 0;
    picker._triggerClosed = true;
    media.isDragOver = false;
    historyCursor = -1;
    navWorkingCopies = {};
    resetInputHeight();
    void submit(candidateSnapshot);
  };
</script>

<form
  class="input-area"
  class:drag-over={media.isDragOver}
  aria-label={t('chat.composerLabel', 'Message')}
  ondragover={media.handleDragOver}
  ondragleave={media.handleDragLeave}
  ondrop={media.handleDrop}
  onsubmit={(event) => {
    event.preventDefault();
    submit();
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
      <p class="composer-toast-title">{t('errors.appError', 'Error')}</p>
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
      onHover={(index) => {
        picker.activeSkillIndex = index;
      }}
    />
  {/if}
  {#if picker.showFileAutocomplete}
    <FileAutocomplete
      bind:this={picker.fileAutocompleteElement}
      files={picker.fileCandidates ?? []}
      query={picker.autocompleteQuery}
      truncated={picker.fileListTruncated}
      loading={picker.fileListLoading}
      activeIndex={picker.activeSkillIndex}
      onSelect={picker.selectFile}
      onHover={(index) => {
        picker.activeSkillIndex = index;
      }}
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
      onHover={(index) => {
        picker.activeSkillIndex = index;
      }}
    />
  {/if}
  {#if media.voiceBusy}
    <div
      class="composer-voice-status"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      <span class="voice-spinner" aria-hidden="true"></span>
      <span>{media.voiceStatus}</span>
      {#if media.transcriptionProgress.elapsed_seconds > 0}
        <span aria-hidden="true"
          >{media.transcriptionProgress.elapsed_seconds}s</span
        >
      {/if}
    </div>
  {/if}
  <div
    class="input-wrap"
    role="group"
    aria-label={t('chat.composerArea', 'Message composer')}
    use:focusInputFromWrapAction
  >
    <textarea
      bind:this={inputElement}
      bind:value={content}
      class="msg-input"
      {disabled}
      aria-label={t('chat.composerLabel', 'Message')}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onpaste={media.handlePaste}
      onclick={handleSelection}
      onkeyup={handleSelection}
      placeholder={t(
        'chat.composerPlaceholder',
        'Ask this agent to do something… (/ for commands, $ for skills, @ for files)',
      )}
      rows="1"></textarea>
    {#if contextFillRatio !== null}
      <span class="context-ring">
        <button
          type="button"
          class="context-ring-trigger"
          aria-label={t('chat.contextRingLabel', 'Context window usage')}
        >
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <circle
              class="context-ring__track"
              cx="8"
              cy="8"
              r={CONTEXT_RING_RADIUS}
              fill="none"
              stroke="currentColor"
              stroke-width="2.5"
            />
            <circle
              class="context-ring__fill"
              cx="8"
              cy="8"
              r={CONTEXT_RING_RADIUS}
              fill="none"
              stroke="currentColor"
              stroke-width="2.5"
              stroke-linecap="round"
              stroke-dasharray={CONTEXT_RING_CIRCUMFERENCE}
              stroke-dashoffset={contextRingOffset}
              transform="rotate(-90 8 8)"
            />
          </svg>
        </button>
        <div class="context-hover-card" use:floatingHoverCard>
          <div class="context-hover-details">{contextTooltip}</div>
          <Button
            variant="secondary"
            disabled={compactionState !== 'idle' || compactionSubmitting}
            onClick={onForceCompaction}
          >
            {compactionState === 'pending'
              ? t('chat.compactionPending', 'Compaction requested…')
              : compactionState === 'running'
                ? t('chat.compactionRunning', 'Compacting…')
                : t('chat.forceCompaction', 'Force compaction')}
          </Button>
        </div>
      </span>
    {/if}
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
        ariaLabel={t('chat.attachment.addFile', 'Add file')}
        tooltip={t('chat.attachment.addFile', 'Add file')}
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
             run stays cancellable even while the input is locked. -->
        <Button
          variant="danger"
          icon
          class="composer-stop"
          disabled={cancelling}
          ariaLabel={cancelling
            ? t('cancel.cancelling', 'Cancelling run…')
            : t('chat.cancelRun', 'Cancel run')}
          tooltip={cancelling
            ? t('cancel.cancelling', 'Cancelling run…')
            : t('chat.cancelRun', 'Cancel run')}
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
      {/if}
      <Button
        type="submit"
        variant="primary"
        icon
        disabled={disabled ||
          submitInFlight ||
          media.hasUploadingAttachments ||
          media.voiceBusy ||
          (!content.trim() && media.pendingAttachments.length === 0)}
        ariaLabel={isRunning
          ? t('chat.queueMessage', 'Queue message')
          : t('chat.sendMessage', 'Send message')}
        tooltip={isRunning
          ? t('chat.queueMessage', 'Queue message')
          : t('chat.sendMessage', 'Send message')}
      >
        <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
          <path d="M12 7L2 2l2 5-2 5 10-5z" fill="currentColor" stroke="none" />
        </svg>
      </Button>
    </div>
  </div>
  {#if media.pendingAttachments.length > 0}
    <div
      class="attachment-tray"
      aria-label={t('chat.attachment.preview', 'Preview attachment')}
    >
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
              aria-label={t('chat.attachment.preview', 'Preview attachment')}
              use:tooltip={t('chat.attachment.preview', 'Preview attachment')}
            >
              <img
                src={attachment.preview_url}
                alt={attachment.filename}
                class="attachment-thumb"
              />
            </button>
            <div
              class="attachment-hover-preview"
              aria-hidden="true"
              use:floatingHoverCard={{ accessible: false }}
            >
              <img
                src={attachment.preview_url}
                alt=""
                class="attachment-hover-image"
              />
            </div>
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
            <span class="attachment-name" use:tooltip={attachment.filename}
              >{attachment.filename}</span
            >
            {#if attachment.uploading}
              <span class="attachment-status">
                {t('chat.attachment.uploading', 'Uploading…')}
              </span>
            {:else if !media.hasImageMediaType(attachment.media_type)}
              <span class="attachment-status">
                {t('chat.attachment.fileLabel', 'Attached file')}
              </span>
            {/if}
          </div>
          <button
            type="button"
            class="attachment-remove"
            aria-label={t('chat.attachment.remove', 'Remove attachment')}
            use:tooltip={t('chat.attachment.remove', 'Remove attachment')}
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
</form>
