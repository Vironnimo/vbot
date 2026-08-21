<script>
  import { onDestroy, tick } from 'svelte';

  import { transcribeSpeech, uploadAttachment } from '$lib/api.js';
  import { createAudioRecorder } from '$lib/audioRecorder.js';
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
    fuzzyFilterFiles,
    isMentionTokenChar,
    matchMentionCandidates,
  } from '$lib/fileMentions.js';
  import { t } from '$lib/i18n.js';
  import {
    buildModelSelectOptions,
    filterModelSelectOptions,
    modelFilterFooterLabel,
  } from '$lib/modelSelection.js';
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';
  import FileAutocomplete from './FileAutocomplete.svelte';
  import ModelAutocomplete from './ModelAutocomplete.svelte';
  import SkillAutocomplete from './SkillAutocomplete.svelte';
  import Button from './ui/Button.svelte';

  const SKILL_TRIGGER_PATTERN = /[A-Za-z0-9_-]/u;
  // Mirrors FileAutocomplete's render cap so keyboard navigation and the
  // rendered list can never disagree on the match set.
  const MAX_FILE_MATCHES = 50;
  const ATTACHMENT_ACCEPT =
    'image/*,audio/*,video/*,text/*,application/pdf,application/msword,application/vnd.ms-excel,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation';

  let {
    disabled = false,
    isRunning = false,
    cancelling = false,
    availableSkills = [],
    draftKey = '',
    historyKey = '',
    focusRequest = 0,
    onSendMessage,
    onCancelRun = () => {},
    onTranscriptionError,
    onListFiles = null,
    onLoadModelCatalog = null,
  } = $props();
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
  let autocompleteElement = $state(null);
  let fileAutocompleteElement = $state(null);
  let modelAutocompleteElement = $state(null);
  let fileInputElement = $state(null);
  let triggerContext = $state(null);
  let activeSkillIndex = $state(0);
  // @-mention picker data: `null` = never fetched for this session. Fetched
  // once per picker open (fresh list, no cache-invalidation problem) and reused
  // at submit to decide which @-tokens are real files.
  let fileCandidates = $state(null);
  let fileListTruncated = $state(false);
  let fileListLoading = $state(false);
  let _fileFetchToken = 0;
  // /model argument autocomplete: `null` = never fetched. Fetched once when the
  // `/model ` trigger opens and reused while the popup stays active.
  let modelCatalog = $state(null);
  let modelCatalogLoading = $state(false);
  let _modelCatalogFetchToken = 0;
  let showAllModels = $state(false);
  let pendingAttachments = $state([]);
  let isDragOver = $state(false);
  let attachmentToastMessage = $state('');
  let recordingState = $state('idle');
  let inputOrigin = $state('');
  let submitInFlight = $state(false);
  let activeRecorder = null;
  let attachmentToastTimeoutId = null;
  let _suppressSelectionUpdate = false;
  let _triggerClosed = false;

  let triggerItems = $derived(availableSkills.filter((item) => item?.name));
  let autocompleteItems = $derived.by(() =>
    triggerItemsForContext(triggerContext),
  );
  let autocompleteQuery = $derived.by(() => {
    if (!triggerContext) {
      return '';
    }

    return content.slice(triggerContext.start + 1, triggerContext.end);
  });
  let matchingFiles = $derived.by(() =>
    triggerContext?.marker === '@'
      ? fuzzyFilterFiles(
          fileCandidates ?? [],
          autocompleteQuery,
          MAX_FILE_MATCHES,
        )
      : [],
  );
  let showSkillAutocomplete = $derived(
    Boolean(triggerContext) &&
      triggerContext.marker !== '@' &&
      triggerContext.marker !== 'model' &&
      matchingSkillCount() > 0,
  );
  let showFileAutocomplete = $derived(
    Boolean(triggerContext) &&
      triggerContext.marker === '@' &&
      (fileListLoading || matchingFiles.length > 0),
  );
  let allModelOptions = $derived.by(() => {
    if (!modelCatalog) {
      return [];
    }
    return buildModelSelectOptions({
      models: modelCatalog.models,
      connections: modelCatalog.connections,
      translate: t,
    }).filter((option) => option.value !== '');
  });
  let modelOptions = $derived(
    filterModelSelectOptions(allModelOptions, { showAll: showAllModels }),
  );
  let modelFilterFooter = $derived(
    modelFilterFooterLabel({
      showAll: showAllModels,
      hiddenCount: allModelOptions.length - modelOptions.length,
      translate: t,
    }),
  );
  let showModelAutocomplete = $derived(
    Boolean(triggerContext) &&
      triggerContext.marker === 'model' &&
      (modelCatalogLoading || matchingModelCount() > 0),
  );
  let hasUploadingAttachments = $derived(
    pendingAttachments.some((attachment) => attachment.uploading),
  );
  let voiceBusy = $derived(
    recordingState === 'requesting' || recordingState === 'transcribing',
  );
  let isRecording = $derived(recordingState === 'recording');

  onDestroy(() => {
    if (attachmentToastTimeoutId !== null) {
      clearTimeout(attachmentToastTimeoutId);
      attachmentToastTimeoutId = null;
    }
    cancelActiveRecording();
    clearPendingAttachments();
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
    historyCursor = -1;
    navWorkingCopies = {};
    inputOrigin = '';
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = false;
    // A different session may sit on a different cwd — drop the file list.
    fileCandidates = null;
    fileListTruncated = false;
    fileListLoading = false;
    _fileFetchToken += 1;
    // Drop the model catalog so a fresh `/model ` fetches the latest list.
    modelCatalog = null;
    modelCatalogLoading = false;
    _modelCatalogFetchToken += 1;
    showAllModels = false;
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

  const safeRevokeObjectUrl = (objectUrl) => {
    if (
      typeof objectUrl === 'string' &&
      objectUrl.startsWith('blob:') &&
      typeof URL !== 'undefined' &&
      typeof URL.revokeObjectURL === 'function'
    ) {
      URL.revokeObjectURL(objectUrl);
    }
  };

  const clearPendingAttachments = () => {
    for (const attachment of pendingAttachments) {
      safeRevokeObjectUrl(attachment.preview_url);
    }
    pendingAttachments = [];
  };

  const showComposerErrorToast = (message) => {
    attachmentToastMessage = message;
    if (attachmentToastTimeoutId !== null) {
      clearTimeout(attachmentToastTimeoutId);
    }
    attachmentToastTimeoutId = setTimeout(() => {
      attachmentToastMessage = '';
      attachmentToastTimeoutId = null;
    }, 3500);
  };

  const showAttachmentUploadErrorToast = () => {
    showComposerErrorToast(
      t('chat.attachment.uploadFailed', 'Attachment upload failed.'),
    );
  };

  const showTranscriptionError = (message) => {
    const normalizedMessage =
      typeof message === 'string' && message.length > 0
        ? message
        : t('chat.voice.transcriptionFailed', 'Speech transcription failed.');
    showComposerErrorToast(normalizedMessage);
    onTranscriptionError?.(normalizedMessage);
  };

  const removePendingAttachmentByPreviewUrl = (previewUrl) => {
    const existingAttachment = pendingAttachments.find(
      (attachment) => attachment.preview_url === previewUrl,
    );
    if (!existingAttachment) {
      return;
    }
    safeRevokeObjectUrl(existingAttachment.preview_url);
    pendingAttachments = pendingAttachments.filter(
      (attachment) => attachment.preview_url !== previewUrl,
    );
  };

  const buildPastedImageFileName = () => {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, '0');
    const date = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    const time = `${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`;
    return `screenshot-${date}-${time}.png`;
  };

  const hasImageMediaType = (mediaType) =>
    typeof mediaType === 'string' &&
    mediaType.toLowerCase().startsWith('image/');

  const hasMediaMediaType = (mediaType) =>
    typeof mediaType === 'string' &&
    /^(image|audio|video)\//.test(mediaType.toLowerCase());

  const _removeAttachment = (index) => {
    const attachment = pendingAttachments[index];
    if (!attachment) {
      return;
    }
    safeRevokeObjectUrl(attachment.preview_url);
    pendingAttachments = pendingAttachments.filter(
      (_, candidateIndex) => candidateIndex !== index,
    );
  };

  const _handleFiles = async (files) => {
    if (disabled) {
      return;
    }

    const selectedFiles = Array.from(files ?? []).filter(Boolean);
    if (selectedFiles.length === 0) {
      return;
    }

    const uploadTasks = selectedFiles.map(async (file) => {
      const previewUrl =
        typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function'
          ? URL.createObjectURL(file)
          : '';
      const pendingAttachment = {
        attachment_id: '',
        filename:
          typeof file.name === 'string' && file.name.trim().length > 0
            ? file.name
            : 'upload.bin',
        media_type:
          typeof file.type === 'string' && file.type.trim().length > 0
            ? file.type
            : 'application/octet-stream',
        preview_url: previewUrl,
        uploading: true,
      };

      pendingAttachments = [...pendingAttachments, pendingAttachment];

      try {
        const result = await uploadAttachment(file);
        pendingAttachments = pendingAttachments.map((attachment) => {
          if (attachment.preview_url !== previewUrl) {
            return attachment;
          }
          return {
            ...attachment,
            attachment_id: result.attachment_id,
            filename: result.filename,
            media_type: result.media_type,
            uploading: false,
          };
        });
      } catch {
        removePendingAttachmentByPreviewUrl(previewUrl);
        showAttachmentUploadErrorToast();
      }
    });

    await Promise.all(uploadTasks);
  };

  const handleFilePickerClick = () => {
    if (disabled) {
      return;
    }
    fileInputElement?.click();
  };

  const handleMicrophoneClick = async () => {
    if (disabled || voiceBusy) {
      return;
    }

    if (isRecording) {
      await stopRecordingAndTranscribe();
      return;
    }

    recordingState = 'requesting';
    try {
      activeRecorder = await createAudioRecorder();
      activeRecorder.start();
      recordingState = 'recording';
    } catch (error) {
      try {
        activeRecorder?.cancel?.();
      } catch {
        // The recorder implementation remains responsible for track cleanup.
      }
      activeRecorder = null;
      recordingState = 'idle';
      showTranscriptionError(
        `${t('chat.voice.startFailed', 'Microphone recording could not start.')} ${error.message ?? ''}`.trim(),
      );
    }
  };

  const stopRecordingAndTranscribe = async () => {
    const recorder = activeRecorder;
    if (!recorder) {
      recordingState = 'idle';
      return;
    }

    activeRecorder = null;
    recordingState = 'transcribing';
    try {
      const audioBlob = await recorder.stop();
      const result = await transcribeSpeech(audioBlob, {
        filename:
          typeof recorder.filename === 'function'
            ? recorder.filename()
            : 'recording.webm',
      });
      await insertTranscript(result.text);
    } catch (error) {
      try {
        recorder.cancel?.();
      } catch {
        // Preserve the transcription error; cleanup was already requested.
      }
      showTranscriptionError(
        `${t('chat.voice.transcriptionFailed', 'Speech transcription failed.')} ${error.message ?? ''}`.trim(),
      );
    } finally {
      recordingState = 'idle';
    }
  };

  const cancelActiveRecording = () => {
    if (!activeRecorder) {
      return;
    }
    try {
      activeRecorder.cancel?.();
    } catch {
      // Track cleanup remains best-effort during component teardown.
    } finally {
      activeRecorder = null;
      recordingState = 'idle';
    }
  };

  const insertTranscript = async (transcript) => {
    const text = typeof transcript === 'string' ? transcript.trim() : '';
    if (!text) {
      return;
    }
    content = content.trim() ? `${content.trimEnd()}\n${text}` : text;
    inputOrigin = 'speech_transcription';
    noteContentEdited();
    triggerContext = null;
    activeSkillIndex = 0;
    await tick();
    inputElement?.focus();
    resizeInput();
  };

  const handleFilePickerChange = async (event) => {
    const input = event.currentTarget;
    const files = input?.files;
    await _handleFiles(files);
    if (input) {
      input.value = '';
    }
  };

  const handlePaste = async (event) => {
    const clipboardItems = Array.from(event.clipboardData?.items ?? []);
    const pastedImageFiles = clipboardItems
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter(Boolean)
      .map((file) => {
        if (typeof file.name === 'string' && file.name.trim().length > 0) {
          return file;
        }
        return new File([file], buildPastedImageFileName(), {
          type: file.type || 'image/png',
          lastModified: Date.now(),
        });
      });

    if (pastedImageFiles.length === 0) {
      return;
    }

    event.preventDefault();
    await _handleFiles(pastedImageFiles);
  };

  const handleDragOver = (event) => {
    event.preventDefault();
    if (!disabled) {
      isDragOver = true;
    }
  };

  const handleDragLeave = (event) => {
    const host = event.currentTarget;
    const relatedTarget = event.relatedTarget;
    if (host?.contains?.(relatedTarget)) {
      return;
    }
    isDragOver = false;
  };

  const handleDrop = async (event) => {
    event.preventDefault();
    isDragOver = false;

    if (disabled) {
      return;
    }

    const files = event.dataTransfer?.files;
    await _handleFiles(files);
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
          fileCandidates = files;
          fileListTruncated = Boolean(result?.truncated);
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
    fileCandidates: fileCandidates === null ? null : Array.from(fileCandidates),
    listFiles: onListFiles,
    sendMessage: onSendMessage,
    attachments: pendingAttachments.map((attachment) => ({
      source: attachment,
      attachment_id: attachment.attachment_id,
      filename: attachment.filename,
      media_type: attachment.media_type,
    })),
  });

  const submitBlocked = (snapshot) =>
    disabled ||
    submitInFlight ||
    hasUploadingAttachments ||
    voiceBusy ||
    (!snapshot.trimmedContent && snapshot.attachments.length === 0);

  const submit = async (snapshot = createSubmitSnapshot()) => {
    if (submitBlocked(snapshot)) {
      return;
    }

    submitInFlight = true;
    cancelActiveRecording();
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
          if (hasMediaMediaType(attachment.media_type)) {
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

    let sent = false;
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
    const submittedAttachmentsStillPresent = pendingAttachments.filter(
      (attachment) => submittedAttachmentSources.has(attachment),
    );
    for (const attachment of submittedAttachmentsStillPresent) {
      safeRevokeObjectUrl(attachment.preview_url);
    }
    pendingAttachments = pendingAttachments.filter(
      (attachment) => !submittedAttachmentSources.has(attachment),
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
      triggerContext = null;
      activeSkillIndex = 0;
      isDragOver = false;
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
    triggerContext = null;
    activeSkillIndex = 0;
    // Don't auto-open the skill popup from recalled text that begins with `/`.
    _triggerClosed = true;
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
  const activeAutocompleteElement = () =>
    showFileAutocomplete
      ? fileAutocompleteElement
      : showModelAutocomplete
        ? modelAutocompleteElement
        : autocompleteElement;
  const activeMatchCount = () => {
    if (triggerContext?.marker === '@') {
      return matchingFiles.length;
    }
    if (triggerContext?.marker === 'model') {
      return matchingModelCount();
    }
    return matchingSkillCount();
  };

  const handleKeydown = (event) => {
    const autocompleteOpen =
      showSkillAutocomplete || showFileAutocomplete || showModelAutocomplete;
    if (autocompleteOpen) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        _suppressSelectionUpdate = true;
        activeSkillIndex = Math.min(
          activeSkillIndex + 1,
          activeMatchCount() - 1,
        );
        return;
      }

      if (event.key === 'ArrowUp') {
        event.preventDefault();
        _suppressSelectionUpdate = true;
        activeSkillIndex = Math.max(activeSkillIndex - 1, 0);
        return;
      }

      if (event.key === 'Tab') {
        if (activeAutocompleteElement()?.selectActive()) {
          event.preventDefault();
        }
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        _triggerClosed = true;
        triggerContext = null;
        activeSkillIndex = 0;
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

    if (autocompleteOpen && activeAutocompleteElement()?.selectActive()) {
      event.preventDefault();
      return;
    }

    event.preventDefault();
    submit();
  };

  const handleInput = () => {
    _triggerClosed = false;
    if (!content.trim()) {
      inputOrigin = '';
    }
    noteContentEdited();
    resizeInput();
    updateTriggerContext();
  };

  const handleSelection = () => {
    if (_suppressSelectionUpdate) {
      _suppressSelectionUpdate = false;
      return;
    }

    updateTriggerContext();
  };

  const matchingSkillCount = () => {
    if (!triggerContext) {
      return 0;
    }

    const normalizedQuery = autocompleteQuery.trim().toLowerCase();
    const matchingItems = normalizedQuery
      ? autocompleteItems.filter((item) =>
          `${item.name} ${item.description ?? ''}`
            .toLowerCase()
            .includes(normalizedQuery),
        )
      : autocompleteItems;

    // Mirror SkillAutocomplete's match set exactly (same predicate, no cap) so
    // arrow-key navigation can reach every rendered entry — the popup shows all
    // matches (scrollable), and the keyboard must not stop short of the list.
    return matchingItems.length;
  };

  // Mirror ModelAutocomplete's match set exactly (same predicate) so keyboard
  // navigation and the rendered list never disagree.
  const matchingModelCount = () => {
    if (!triggerContext || triggerContext.marker !== 'model') {
      return 0;
    }

    const normalizedQuery = autocompleteQuery.trim().toLowerCase();
    if (!normalizedQuery) {
      return modelOptions.length;
    }

    return modelOptions.filter((option) =>
      `${option.label} ${option.secondaryLabel ?? ''}`
        .toLowerCase()
        .includes(normalizedQuery),
    ).length;
  };

  function triggerItemsForContext(context) {
    if (!context) {
      return [];
    }

    if (context.marker === '$') {
      return triggerItems.filter((item) => item.type !== 'command');
    }

    return triggerItems;
  }

  const updateTriggerContext = () => {
    if (_triggerClosed) {
      return;
    }

    if (!inputElement) {
      triggerContext = null;
      activeSkillIndex = 0;
      return;
    }

    const cursorPosition = inputElement.selectionStart ?? content.length;
    const previousContext = triggerContext;
    const skillTrigger = detectSkillTrigger(content, cursorPosition);
    const modelTrigger = skillTrigger
      ? null
      : detectModelArgumentTrigger(content, cursorPosition);
    triggerContext = skillTrigger ?? modelTrigger;
    activeSkillIndex = 0;

    // Reset show-all when leaving the model trigger.
    if (
      previousContext?.marker === 'model' &&
      triggerContext?.marker !== 'model'
    ) {
      showAllModels = false;
    }

    // A newly opened @-picker (or the caret jumping to a different @-token)
    // fetches a fresh file list; typing within the same token filters locally.
    if (
      triggerContext?.marker === '@' &&
      (previousContext?.marker !== '@' ||
        previousContext.start !== triggerContext.start)
    ) {
      refreshFileCandidates();
    }

    // A newly opened /model argument popup fetches the model catalog once;
    // typing within the same argument filters locally.
    if (
      triggerContext?.marker === 'model' &&
      previousContext?.marker !== 'model'
    ) {
      refreshModelCatalog();
    }
  };

  const refreshFileCandidates = async () => {
    if (typeof onListFiles !== 'function') {
      fileCandidates = [];
      fileListTruncated = false;
      return;
    }
    // The token invalidates stale responses: a session switch or a newer fetch
    // bumps it, and the slower response is dropped instead of applied.
    const fetchToken = ++_fileFetchToken;
    fileListLoading = true;
    try {
      const result = await onListFiles();
      if (fetchToken !== _fileFetchToken) {
        return;
      }
      fileCandidates = Array.isArray(result?.files) ? result.files : [];
      fileListTruncated = Boolean(result?.truncated);
    } catch {
      if (fetchToken !== _fileFetchToken) {
        return;
      }
      // Keep whatever list we had; a picker without data simply shows nothing.
      fileCandidates = fileCandidates ?? [];
      fileListTruncated = false;
    } finally {
      if (fetchToken === _fileFetchToken) {
        fileListLoading = false;
      }
    }
  };

  const refreshModelCatalog = async () => {
    if (typeof onLoadModelCatalog !== 'function') {
      modelCatalog = { models: [], connections: [] };
      return;
    }
    // The token invalidates stale responses: a session switch or a newer fetch
    // bumps it, and the slower response is dropped instead of applied.
    const fetchToken = ++_modelCatalogFetchToken;
    modelCatalogLoading = true;
    try {
      const result = await onLoadModelCatalog();
      if (fetchToken !== _modelCatalogFetchToken) {
        return;
      }
      modelCatalog = {
        models: Array.isArray(result?.models) ? result.models : [],
        connections: Array.isArray(result?.connections)
          ? result.connections
          : [],
      };
    } catch {
      if (fetchToken !== _modelCatalogFetchToken) {
        return;
      }
      modelCatalog = modelCatalog ?? { models: [], connections: [] };
    } finally {
      if (fetchToken === _modelCatalogFetchToken) {
        modelCatalogLoading = false;
      }
    }
  };

  // A /model argument trigger: content starts with "/model" followed by a
  // space, and the cursor sits at or after that space. The query is everything
  // after the space (extracted via the shared autocompleteQuery derived).
  const detectModelArgumentTrigger = (value, cursorPosition) => {
    const boundedCursor = Math.max(0, Math.min(cursorPosition, value.length));

    if (!value.startsWith('/model')) {
      return null;
    }

    if (value.length <= 6 || value[6] !== ' ') {
      return null;
    }

    if (boundedCursor < 7) {
      return null;
    }

    return { marker: 'model', start: 6, end: boundedCursor };
  };
  const detectFileTrigger = (value, boundedCursor) => {
    let start = boundedCursor - 1;

    while (start >= 0 && isMentionTokenChar(value[start])) {
      start -= 1;
    }

    if (start < 0 || value[start] !== '@') {
      return null;
    }

    if (start > 0) {
      const previous = value[start - 1];
      if (isMentionTokenChar(previous) || previous === '@') {
        return null;
      }
    }

    return { marker: '@', start, end: boundedCursor };
  };

  const detectSkillTrigger = (value, cursorPosition) => {
    const boundedCursor = Math.max(0, Math.min(cursorPosition, value.length));

    const fileTrigger = detectFileTrigger(value, boundedCursor);
    if (fileTrigger) {
      return fileTrigger;
    }

    let start = boundedCursor - 1;

    while (start >= 0 && SKILL_TRIGGER_PATTERN.test(value[start])) {
      start -= 1;
    }

    if (start < 0) {
      return null;
    }

    const trigger = value[start];

    if (trigger !== '/' && trigger !== '$') {
      return null;
    }

    if (trigger === '/' && start !== 0) {
      return null;
    }

    if (
      trigger === '$' &&
      start > 0 &&
      SKILL_TRIGGER_PATTERN.test(value[start - 1])
    ) {
      return null;
    }

    for (let index = start + 1; index < boundedCursor; index += 1) {
      if (!SKILL_TRIGGER_PATTERN.test(value[index])) {
        return null;
      }
    }

    return { marker: trigger, start, end: boundedCursor };
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
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;
    isDragOver = false;
    historyCursor = -1;
    navWorkingCopies = {};
    resetInputHeight();
    void submit(candidateSnapshot);
  };

  const selectFile = async (file) => {
    if (!triggerContext || typeof file !== 'string' || !file) {
      return;
    }

    const prefix = content.slice(0, triggerContext.start);
    const suffix = content.slice(triggerContext.end);
    // The trailing space ends the mention token, so typing continues normally.
    const insertedToken = `@${file} `;
    const nextCursorPosition = prefix.length + insertedToken.length;
    content = `${prefix}${insertedToken}${suffix}`;
    noteContentEdited();
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;

    await tick();
    inputElement?.focus();
    inputElement?.setSelectionRange(nextCursorPosition, nextCursorPosition);
    resizeInput();
  };

  const selectSkill = async (skill) => {
    if (!triggerContext || !skill?.name) {
      return;
    }

    if (
      triggerContext.marker === '/' &&
      skill.type === 'command' &&
      skill.argument === 'none'
    ) {
      executeImmediateCommand(skill);
      return;
    }

    const prefix = content.slice(0, triggerContext.start);
    const suffix = content.slice(triggerContext.end);
    const marker = triggerContext.marker;
    const stripPattern = marker === '/' ? /^\/+/ : /^\$+/;
    const normalizedSkillName = String(skill.name).replace(stripPattern, '');
    if (!normalizedSkillName) {
      return;
    }
    const insertedToken = `${marker}${normalizedSkillName}`;
    const nextCursorPosition = prefix.length + insertedToken.length;
    content = `${prefix}${insertedToken}${suffix}`;
    noteContentEdited();
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;

    await tick();
    inputElement?.focus();
    inputElement?.setSelectionRange(nextCursorPosition, nextCursorPosition);
    resizeInput();
  };

  // A model chosen from the /model argument popup is submitted immediately —
  // no second Enter is needed. The option's canonical value (including any
  // connection/account suffix) becomes the /model argument through the regular
  // guarded submit path so failures leave a retryable draft.
  const selectModel = (option) => {
    if (!triggerContext || !option?.value) {
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
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;
    isDragOver = false;
    historyCursor = -1;
    navWorkingCopies = {};
    resetInputHeight();
    void submit(candidateSnapshot);
  };
</script>

<form
  class="input-area"
  class:drag-over={isDragOver}
  aria-label={t('chat.composerLabel', 'Message')}
  ondragover={handleDragOver}
  ondragleave={handleDragLeave}
  ondrop={handleDrop}
  onsubmit={(event) => {
    event.preventDefault();
    submit();
  }}
>
  <input
    bind:this={fileInputElement}
    class="attachment-file-input"
    type="file"
    accept={ATTACHMENT_ACCEPT}
    multiple
    {disabled}
    onchange={handleFilePickerChange}
  />
  {#if attachmentToastMessage}
    <div class="composer-toast" role="status" aria-live="polite">
      <p class="composer-toast-title">{t('errors.appError', 'Error')}</p>
      <p class="composer-toast-message">{attachmentToastMessage}</p>
    </div>
  {/if}
  {#if showSkillAutocomplete}
    <SkillAutocomplete
      bind:this={autocompleteElement}
      skills={autocompleteItems}
      query={autocompleteQuery}
      marker={triggerContext.marker}
      activeIndex={activeSkillIndex}
      onSelect={selectSkill}
      onHover={(index) => {
        activeSkillIndex = index;
      }}
    />
  {/if}
  {#if showFileAutocomplete}
    <FileAutocomplete
      bind:this={fileAutocompleteElement}
      files={fileCandidates ?? []}
      query={autocompleteQuery}
      truncated={fileListTruncated}
      loading={fileListLoading}
      activeIndex={activeSkillIndex}
      onSelect={selectFile}
      onHover={(index) => {
        activeSkillIndex = index;
      }}
    />
  {/if}
  {#if showModelAutocomplete}
    <ModelAutocomplete
      bind:this={modelAutocompleteElement}
      options={modelOptions}
      query={autocompleteQuery}
      loading={modelCatalogLoading}
      footerLabel={modelFilterFooter}
      onFooterAction={() => (showAllModels = !showAllModels)}
      activeIndex={activeSkillIndex}
      onSelect={selectModel}
      onHover={(index) => {
        activeSkillIndex = index;
      }}
    />
  {/if}
  <div
    class="input-wrap"
    role="group"
    aria-label={t('chat.composerArea', 'Message composer')}
    use:focusInputFromWrapAction
  >
    <textarea
      id="chat-composer-input"
      bind:this={inputElement}
      bind:value={content}
      class="msg-input"
      {disabled}
      aria-label={t('chat.composerLabel', 'Message')}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onpaste={handlePaste}
      onclick={handleSelection}
      onkeyup={handleSelection}
      placeholder={t(
        'chat.composerPlaceholder',
        'Ask this agent to do something… (/ for commands, $ for skills, @ for files)',
      )}
      rows="1"
    ></textarea>
    <div class="input-btns">
      <Button
        variant="tertiary"
        icon
        class={isRecording ? 'btn-icon--active' : ''}
        disabled={disabled || voiceBusy}
        ariaLabel={isRecording
          ? t('chat.voice.stopRecording', 'Stop recording')
          : t('chat.voice.startRecording', 'Start voice input')}
        tooltip={isRecording
          ? t('chat.voice.stopRecording', 'Stop recording')
          : t('chat.voice.startRecording', 'Start voice input')}
        onClick={handleMicrophoneClick}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M8 2a2 2 0 0 1 2 2v4a2 2 0 1 1-4 0V4a2 2 0 0 1 2-2z" />
          <path d="M4 7v1a4 4 0 0 0 8 0V7M8 12v2M6 14h4" />
        </svg>
      </Button>
      <Button
        variant="tertiary"
        icon
        {disabled}
        ariaLabel={t('chat.attachment.addFile', 'Add file')}
        tooltip={t('chat.attachment.addFile', 'Add file')}
        onClick={handleFilePickerClick}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
          />
        </svg>
      </Button>
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
          hasUploadingAttachments ||
          voiceBusy ||
          (!content.trim() && pendingAttachments.length === 0)}
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
  {#if pendingAttachments.length > 0}
    <div
      class="attachment-tray"
      aria-label={t('chat.attachment.preview', 'Preview attachment')}
    >
      {#each pendingAttachments as attachment, index (`${attachment.preview_url}-${index}`)}
        <div
          class="attachment-item"
          class:attachment-item-image={hasImageMediaType(attachment.media_type)}
        >
          {#if hasImageMediaType(attachment.media_type)}
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
            {:else if !hasImageMediaType(attachment.media_type)}
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
            onclick={() => _removeAttachment(index)}
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

<style>
  .input-area {
    position: relative;
    width: 100%;
    min-width: 0;
  }

  /* Center the composer on the same axis as the capped message column. The
     unbordered `.input-area` stays full-width on the chat background; only the
     input box and attachment tray are capped to `--chat-measure` and centered.
     With the area's symmetric 20px padding this lines the input's left edge up
     with the message column exactly. `full` disables the cap. */
  .input-wrap,
  .attachment-tray {
    width: 100%;
    max-width: var(--chat-measure);
    margin-inline: auto;
  }

  .msg-input {
    height: 22px;
  }

  /* Extra breathing room around the stop button: cancelling by accident is the
     only costly misclick in this row, so it gets double the row gap on both
     sides (mic/attach and Send are harmless neighbors). */
  :global(.composer-stop) {
    margin-inline: 4px;
  }

  .input-area.drag-over .input-wrap {
    border-color: var(--accent-40);
    box-shadow: var(--focus-ring);
  }

  .attachment-file-input {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    border: 0;
  }

  .composer-toast {
    position: absolute;
    right: 0;
    bottom: calc(100% + 10px);
    z-index: 20;
    min-width: 220px;
    max-width: min(340px, 92vw);
    padding: 10px 12px;
    border: 1px solid rgba(252, 129, 129, 0.35);
    border-left: 2px solid var(--red);
    border-radius: var(--r-md);
    background: var(--surface);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
  }

  .composer-toast-title {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 12.5px;
    font-weight: 600;
    letter-spacing: 0.02em;
  }

  .composer-toast-message {
    margin: 2px 0 0;
    color: var(--text-med);
    font-family: var(--font-ui);
    font-size: 12px;
    line-height: 1.4;
  }

  .attachment-tray {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 8px;
    padding: 10px 2px 0;
  }

  /* The attachment card chrome (.attachment-item, .attachment-thumb,
     .attachment-hover-preview, .attachment-meta, .attachment-name, …) is shared
     with the chat timeline and lives in styles/chat-timeline.css. Only the
     composer-specific controls below (tray, remove button) stay scoped here. */

  .attachment-remove {
    display: flex;
    width: 22px;
    height: 22px;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    border: 1px solid transparent;
    border-radius: var(--r-sm);
    color: var(--text-lo);
    background: transparent;
    transition:
      border-color 120ms ease,
      color 120ms ease,
      background 120ms ease;
  }

  .attachment-remove:hover,
  .attachment-remove:focus-visible {
    border-color: rgba(252, 129, 129, 0.4);
    color: var(--red);
    background: rgba(252, 129, 129, 0.08);
    outline: none;
  }

  .attachment-remove svg {
    width: 12px;
    height: 12px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: 1.4;
  }

  @media (max-width: 640px) {
    .input-area {
      padding: 12px 14px;
    }
  }
</style>
