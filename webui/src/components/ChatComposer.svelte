<script>
  import { onDestroy, tick } from 'svelte';

  import { transcribeSpeech, uploadAttachment } from '$lib/api.js';
  import { createAudioRecorder } from '$lib/audioRecorder.js';
  import { t } from '$lib/i18n.js';
  import SkillAutocomplete from './SkillAutocomplete.svelte';
  import Button from './ui/Button.svelte';

  const SKILL_TRIGGER_PATTERN = /[A-Za-z0-9_-]/u;
  const ATTACHMENT_ACCEPT =
    'image/*,audio/*,video/*,text/*,application/pdf,application/msword,application/vnd.ms-excel,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation';

  let {
    disabled = false,
    isRunning = false,
    availableSkills = [],
    onSendMessage,
    onTranscriptionError,
  } = $props();
  let content = $state('');
  let inputElement = $state(null);
  let autocompleteElement = $state(null);
  let fileInputElement = $state(null);
  let triggerContext = $state(null);
  let activeSkillIndex = $state(0);
  let pendingAttachments = $state([]);
  let isDragOver = $state(false);
  let attachmentToastMessage = $state('');
  let recordingState = $state('idle');
  let inputOrigin = $state('');
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
  let showSkillAutocomplete = $derived(
    Boolean(triggerContext) && matchingSkillCount() > 0,
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

  const hasTextMediaType = (mediaType) =>
    typeof mediaType === 'string' &&
    mediaType.toLowerCase().startsWith('text/');

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
        text_content: null,
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
            text_content: result.text_content ?? null,
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
    activeRecorder.cancel?.();
    activeRecorder = null;
    recordingState = 'idle';
  };

  const insertTranscript = async (transcript) => {
    const text = typeof transcript === 'string' ? transcript.trim() : '';
    if (!text) {
      return;
    }
    content = content.trim() ? `${content.trimEnd()}\n${text}` : text;
    inputOrigin = 'speech_transcription';
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

  const submit = () => {
    const trimmedContent = content.trim();
    const hasPendingAttachments = pendingAttachments.length > 0;

    if (
      disabled ||
      hasUploadingAttachments ||
      voiceBusy ||
      (!trimmedContent && !hasPendingAttachments)
    ) {
      return;
    }

    cancelActiveRecording();
    const sendOptions = inputOrigin ? { inputOrigin } : null;

    if (!hasPendingAttachments) {
      if (sendOptions) {
        onSendMessage?.(content, sendOptions);
      } else {
        onSendMessage?.(content);
      }
    } else {
      const contentBlocks = pendingAttachments
        .filter(
          (attachment) => !attachment.uploading && attachment.attachment_id,
        )
        .map((attachment) => {
          if (hasMediaMediaType(attachment.media_type)) {
            return {
              type: 'media',
              attachment_id: attachment.attachment_id,
              filename: attachment.filename,
              media_type: attachment.media_type,
            };
          }

          if (
            hasTextMediaType(attachment.media_type) &&
            typeof attachment.text_content === 'string'
          ) {
            return {
              type: 'text',
              text: attachment.text_content,
            };
          }

          return {
            type: 'file',
            attachment_id: attachment.attachment_id,
            filename: attachment.filename,
            media_type: attachment.media_type,
          };
        });

      if (trimmedContent) {
        contentBlocks.unshift({ type: 'text', text: trimmedContent });
      }

      if (contentBlocks.length === 0) {
        return;
      }

      if (sendOptions) {
        onSendMessage?.(contentBlocks, sendOptions);
      } else {
        onSendMessage?.(contentBlocks);
      }
      clearPendingAttachments();
    }

    content = '';
    inputOrigin = '';
    triggerContext = null;
    activeSkillIndex = 0;
    isDragOver = false;
    resetInputHeight();
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

  const handleKeydown = (event) => {
    if (showSkillAutocomplete) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        _suppressSelectionUpdate = true;
        activeSkillIndex = Math.min(
          activeSkillIndex + 1,
          matchingSkillCount() - 1,
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
        if (autocompleteElement?.selectActive()) {
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

    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }

    if (showSkillAutocomplete && autocompleteElement?.selectActive()) {
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
    triggerContext = detectSkillTrigger(content, cursorPosition);
    activeSkillIndex = 0;
  };

  const detectSkillTrigger = (value, cursorPosition) => {
    const boundedCursor = Math.max(0, Math.min(cursorPosition, value.length));
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
  // popup — no token is inserted and no second Enter is needed. The partial
  // token the user typed (e.g. `/stat`) is cleared first, mirroring submit()'s
  // reset block, then the canonical command is sent. Backend command dispatch
  // runs before the run path, so this never enters the busy-session queue.
  const executeImmediateCommand = (skill) => {
    const normalizedName = String(skill.name).replace(/^\/+/, '');
    if (!normalizedName) {
      return;
    }
    content = '';
    inputOrigin = '';
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;
    isDragOver = false;
    resetInputHeight();
    onSendMessage?.(`/${normalizedName}`);
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
    triggerContext = null;
    activeSkillIndex = 0;
    _triggerClosed = true;

    await tick();
    inputElement?.focus();
    inputElement?.setSelectionRange(nextCursorPosition, nextCursorPosition);
    resizeInput();
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
        'Ask this agent to do something…',
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
        title={isRecording
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
        title={t('chat.attachment.addFile', 'Add file')}
        onClick={handleFilePickerClick}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
          />
        </svg>
      </Button>
      <Button
        type="submit"
        variant="primary"
        icon
        disabled={disabled ||
          hasUploadingAttachments ||
          voiceBusy ||
          (!content.trim() && pendingAttachments.length === 0)}
        ariaLabel={isRunning
          ? t('chat.queueMessage', 'Queue message')
          : t('chat.sendMessage', 'Send message')}
        title={isRunning
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
              title={t('chat.attachment.preview', 'Preview attachment')}
            >
              <img
                src={attachment.preview_url}
                alt={attachment.filename}
                class="attachment-thumb"
              />
            </button>
            <div class="attachment-hover-preview" aria-hidden="true">
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
            <span class="attachment-name" title={attachment.filename}
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
            title={t('chat.attachment.remove', 'Remove attachment')}
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
     `.input-area` bar stays full-width (border-top + surface fill); only the
     input box and attachment tray are capped to `--chat-measure` and centered.
     With the bar's symmetric 20px padding this lines the input's left edge up
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

  .input-area.drag-over .input-wrap {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
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

  .attachment-item {
    position: relative;
    display: flex;
    min-width: 0;
    max-width: min(320px, 100%);
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface-2);
  }

  .attachment-thumb-trigger {
    flex-shrink: 0;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
  }

  .attachment-thumb {
    display: block;
    width: 56px;
    height: 56px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    object-fit: cover;
    background: var(--bg);
  }

  .attachment-hover-preview {
    position: absolute;
    left: 0;
    bottom: calc(100% + 8px);
    z-index: 15;
    width: min(300px, 72vw);
    padding: 6px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-md);
    background: var(--surface);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.5);
    opacity: 0;
    pointer-events: none;
    transform: translateY(6px);
    transition:
      opacity 140ms ease,
      transform 140ms ease;
  }

  .attachment-item-image:hover .attachment-hover-preview,
  .attachment-item-image:focus-within .attachment-hover-preview {
    opacity: 1;
    transform: translateY(0);
  }

  .attachment-hover-image {
    display: block;
    width: 100%;
    border-radius: var(--r-sm);
    object-fit: contain;
    background: var(--bg);
  }

  .attachment-file-icon {
    display: flex;
    width: 30px;
    height: 30px;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-med);
    background: var(--surface-3);
  }

  .attachment-file-icon svg {
    width: 14px;
    height: 14px;
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 1.3;
  }

  .attachment-meta {
    display: flex;
    min-width: 0;
    flex: 1;
    flex-direction: column;
    gap: 2px;
  }

  .attachment-name {
    overflow: hidden;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 12.5px;
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachment-status {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

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

    .attachment-item {
      max-width: 100%;
    }

    .attachment-hover-preview {
      width: min(260px, calc(100vw - 48px));
    }
  }
</style>
