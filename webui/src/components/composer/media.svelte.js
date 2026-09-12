import { SvelteDate } from 'svelte/reactivity';
import { t } from '$lib/i18n.js';
import {
  getPendingAttachments,
  setPendingAttachments,
} from '$lib/composerMemory.js';
import { uploadAttachment, transcribeSpeech } from '$lib/api.js';
import { createAudioRecorder } from '$lib/audioRecorder.js';

export function createComposerMedia(context) {
  const ATTACHMENT_ACCEPT =
    'image/*,audio/*,video/*,text/*,application/pdf,application/msword,application/vnd.ms-excel,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation';

  const EPHEMERAL_ATTACHMENT_SCOPE = '__composer__';

  let fileInputElement = $state(null);

  let pendingAttachmentsByScope = $state({});

  let nextPendingAttachmentId = 0;

  let pendingAttachments = $derived(
    attachmentsForScope(attachmentScopeForDraftKey(context.draftKey)),
  );

  let isDragOver = $state(false);

  let attachmentToastMessage = $state('');

  let recordingState = $state('idle');

  let transcriptionProgress = $state({
    phase: 'uploading',
    elapsed_seconds: 0,
  });

  let voiceStatus = $derived(
    recordingState === 'requesting'
      ? t('chat.voice.progress.microphone')
      : t(
          `chat.voice.progress.${transcriptionProgress.phase}`,
          t('chat.voice.progress.transcribing'),
        ),
  );

  let microphoneLabel = $derived(
    voiceBusy
      ? voiceStatus
      : isRecording
        ? t('chat.voice.stopRecording', 'Stop recording')
        : t('chat.voice.startRecording', 'Start voice input'),
  );

  let activeRecorder = null;

  let recorderRequestGeneration = 0;

  let destroyed = false;

  let attachmentToastTimeoutId = null;

  let hasUploadingAttachments = $derived(
    pendingAttachments.some((attachment) => attachment.uploading),
  );

  let voiceBusy = $derived(
    recordingState === 'requesting' || recordingState === 'transcribing',
  );

  let isRecording = $derived(recordingState === 'recording');

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

  const attachmentScopeForDraftKey = (key) => key || EPHEMERAL_ATTACHMENT_SCOPE;

  const attachmentsForScope = (scope) => pendingAttachmentsByScope[scope] ?? [];

  const attachmentPreviewUrl = (attachmentId) =>
    `/api/attachments/${encodeURIComponent(attachmentId)}`;

  const nextAttachmentLocalId = () => {
    nextPendingAttachmentId += 1;
    return `pending-attachment-${nextPendingAttachmentId}`;
  };

  const hydratePendingAttachments = (key) => {
    const scope = attachmentScopeForDraftKey(key);
    if (scope in pendingAttachmentsByScope || !key) {
      return;
    }
    const restored = getPendingAttachments(key).map((attachment) => ({
      ...attachment,
      local_id: nextAttachmentLocalId(),
      preview_url: attachmentPreviewUrl(attachment.attachment_id),
      uploading: false,
    }));
    pendingAttachmentsByScope = {
      ...pendingAttachmentsByScope,
      [scope]: restored,
    };
  };

  const setAttachmentsForDraftKey = (key, nextAttachments) => {
    const scope = attachmentScopeForDraftKey(key);
    const next = Array.isArray(nextAttachments) ? nextAttachments : [];
    pendingAttachmentsByScope = {
      ...pendingAttachmentsByScope,
      [scope]: next,
    };
    if (key) {
      setPendingAttachments(key, next);
    }
  };

  const updateAttachmentsForDraftKey = (key, update) => {
    const scope = attachmentScopeForDraftKey(key);
    const current = attachmentsForScope(scope);
    setAttachmentsForDraftKey(key, update(current));
  };

  const releasePendingAttachmentPreviews = () => {
    for (const attachments of Object.values(pendingAttachmentsByScope)) {
      for (const attachment of attachments) {
        safeRevokeObjectUrl(attachment.preview_url);
      }
    }
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
    context.onTranscriptionError?.(normalizedMessage);
  };

  const removePendingAttachmentByLocalId = (key, localId) => {
    const scope = attachmentScopeForDraftKey(key);
    const existingAttachment = attachmentsForScope(scope).find(
      (attachment) => attachment.local_id === localId,
    );
    if (!existingAttachment) {
      return;
    }
    safeRevokeObjectUrl(existingAttachment.preview_url);
    updateAttachmentsForDraftKey(key, (attachments) =>
      attachments.filter((attachment) => attachment.local_id !== localId),
    );
  };

  const buildPastedImageFileName = () => {
    const now = new SvelteDate();
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
    removePendingAttachmentByLocalId(context.draftKey, attachment.local_id);
  };

  const _handleFiles = async (files) => {
    if (context.disabled) {
      return;
    }

    const selectedFiles = Array.from(files ?? []).filter(Boolean);
    if (selectedFiles.length === 0) {
      return;
    }

    const attachmentDraftKey = context.draftKey;
    const newAttachments = selectedFiles.map((file) => {
      const previewUrl =
        typeof URL !== 'undefined' && typeof URL.createObjectURL === 'function'
          ? URL.createObjectURL(file)
          : '';
      return {
        local_id: nextAttachmentLocalId(),
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
    });
    updateAttachmentsForDraftKey(attachmentDraftKey, (attachments) => [
      ...attachments,
      ...newAttachments,
    ]);

    const uploadTasks = selectedFiles.map(async (file, index) => {
      const localId = newAttachments[index].local_id;
      try {
        const result = await uploadAttachment(file);
        updateAttachmentsForDraftKey(attachmentDraftKey, (attachments) =>
          attachments.map((attachment) => {
            if (attachment.local_id !== localId) {
              return attachment;
            }
            return {
              ...attachment,
              attachment_id: result.attachment_id,
              filename: result.filename,
              media_type: result.media_type,
              uploading: false,
            };
          }),
        );
      } catch {
        removePendingAttachmentByLocalId(attachmentDraftKey, localId);
        showAttachmentUploadErrorToast();
      }
    });

    await Promise.all(uploadTasks);
  };

  const handleFilePickerClick = () => {
    if (context.disabled) {
      return;
    }
    fileInputElement?.click();
  };

  const handleMicrophoneClick = async () => {
    if (context.disabled || voiceBusy) {
      return;
    }

    if (isRecording) {
      await stopRecordingAndTranscribe();
      return;
    }

    recordingState = 'requesting';
    const requestGeneration = ++recorderRequestGeneration;
    try {
      const recorder = await createAudioRecorder();
      if (destroyed || requestGeneration !== recorderRequestGeneration) {
        // getUserMedia cannot be aborted once the browser permission prompt is
        // visible. Release tracks immediately when its late result arrives.
        recorder.cancel?.();
        return;
      }
      activeRecorder = recorder;
      activeRecorder.start();
      recordingState = 'recording';
    } catch (error) {
      if (destroyed || requestGeneration !== recorderRequestGeneration) {
        return;
      }
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
    transcriptionProgress = { phase: 'uploading', elapsed_seconds: 0 };
    const requestGeneration = ++recorderRequestGeneration;
    try {
      const audioBlob = await recorder.stop();
      const result = await transcribeSpeech(audioBlob, {
        filename:
          typeof recorder.filename === 'function'
            ? recorder.filename()
            : 'recording.webm',
        onProgress: (progress) => {
          if (!destroyed && requestGeneration === recorderRequestGeneration)
            transcriptionProgress = progress;
        },
      });
      if (destroyed || requestGeneration !== recorderRequestGeneration) return;
      await context.insertTranscript(result.text);
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
    recorderRequestGeneration += 1;
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
    if (!context.disabled) {
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

    if (context.disabled) {
      return;
    }

    const files = event.dataTransfer?.files;
    await _handleFiles(files);
  };
  function destroy() {
    destroyed = true;
    recorderRequestGeneration += 1;
    if (attachmentToastTimeoutId !== null) {
      clearTimeout(attachmentToastTimeoutId);
      attachmentToastTimeoutId = null;
    }
    cancelActiveRecording();
    releasePendingAttachmentPreviews();
  }
  return {
    destroy,
    cancelActiveRecording,
    get ATTACHMENT_ACCEPT() {
      return ATTACHMENT_ACCEPT;
    },
    get fileInputElement() {
      return fileInputElement;
    },
    set fileInputElement(value) {
      fileInputElement = value;
    },
    get pendingAttachments() {
      return pendingAttachments;
    },
    set pendingAttachments(value) {
      pendingAttachments = value;
    },
    get isDragOver() {
      return isDragOver;
    },
    set isDragOver(value) {
      isDragOver = value;
    },
    get attachmentToastMessage() {
      return attachmentToastMessage;
    },
    set attachmentToastMessage(value) {
      attachmentToastMessage = value;
    },
    get transcriptionProgress() {
      return transcriptionProgress;
    },
    set transcriptionProgress(value) {
      transcriptionProgress = value;
    },
    get voiceStatus() {
      return voiceStatus;
    },
    set voiceStatus(value) {
      voiceStatus = value;
    },
    get microphoneLabel() {
      return microphoneLabel;
    },
    set microphoneLabel(value) {
      microphoneLabel = value;
    },
    get hasUploadingAttachments() {
      return hasUploadingAttachments;
    },
    set hasUploadingAttachments(value) {
      hasUploadingAttachments = value;
    },
    get voiceBusy() {
      return voiceBusy;
    },
    set voiceBusy(value) {
      voiceBusy = value;
    },
    get isRecording() {
      return isRecording;
    },
    set isRecording(value) {
      isRecording = value;
    },
    get safeRevokeObjectUrl() {
      return safeRevokeObjectUrl;
    },
    get attachmentScopeForDraftKey() {
      return attachmentScopeForDraftKey;
    },
    get attachmentsForScope() {
      return attachmentsForScope;
    },
    get hydratePendingAttachments() {
      return hydratePendingAttachments;
    },
    get updateAttachmentsForDraftKey() {
      return updateAttachmentsForDraftKey;
    },
    get hasImageMediaType() {
      return hasImageMediaType;
    },
    get hasMediaMediaType() {
      return hasMediaMediaType;
    },
    get _removeAttachment() {
      return _removeAttachment;
    },
    get handleFilePickerClick() {
      return handleFilePickerClick;
    },
    get handleMicrophoneClick() {
      return handleMicrophoneClick;
    },
    get handleFilePickerChange() {
      return handleFilePickerChange;
    },
    get handlePaste() {
      return handlePaste;
    },
    get handleDragOver() {
      return handleDragOver;
    },
    get handleDragLeave() {
      return handleDragLeave;
    },
    get handleDrop() {
      return handleDrop;
    },
  };
}
