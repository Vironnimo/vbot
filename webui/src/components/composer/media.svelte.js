import { t, tOr } from '$lib/i18n.js';
import {
  getPendingAttachments,
  currentMediaGeneration,
  parkDraftMedia,
  takeDraftMedia,
  setPendingAttachments,
  subscribePendingAttachments,
} from '$lib/composerMemory.js';
import {
  getAttachmentUrl,
  prepareSpeechTranscription,
  uploadAttachment,
  transcribeSpeech,
} from '$lib/api.js';
import { createAudioRecorder } from '$lib/audioRecorder.js';
import {
  createMediaTransfer,
  interruptedTransfer,
} from '$lib/api/mediaTransfers.js';
import { onServerReady } from '$lib/connectionState.js';
import {
  dateTimePartsInZone,
  dateTimePrefs,
} from '$lib/dateTimePrefs.svelte.js';

export function createComposerMedia(context) {
  const ownerGeneration = currentMediaGeneration();
  const ATTACHMENT_ACCEPT =
    'image/*,audio/*,video/*,text/*,application/pdf,application/msword,application/vnd.ms-excel,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.presentationml.presentation';

  const EPHEMERAL_ATTACHMENT_SCOPE = '__composer__';

  let fileInputElement = $state(null);

  let pendingAttachmentsByScope = $state({});

  let nextPendingAttachmentId = 0;

  let pendingAttachments = $derived(
    cachedAttachmentsForScope(attachmentScopeForDraftKey(context.draftKey)),
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
      : tOr(
          `chat.voice.progress.${transcriptionProgress.phase}`,
          t('chat.voice.progress.transcribing'),
        ),
  );

  let microphoneLabel = $derived(
    voiceBusy
      ? voiceStatus
      : isRecording
        ? t('chat.voice.stopRecording')
        : t('chat.voice.startRecording'),
  );

  let activeRecorder = null;

  let transcriptionAbort = null;
  let pendingRecording = null;
  let retryRecording = $state(false);
  let recordingOutcomeUnknown = $state(false);
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- contains native File and transport handles, UI state lives in the cards.
  const uploads = new Map();

  let recorderRequestGeneration = 0;

  let destroyed = false;

  let attachmentToastTimeoutId = null;

  let hasUploadingAttachments = $derived(
    pendingAttachments.some((attachment) => !attachment.attachment_id),
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

  const cachedAttachmentsForScope = (scope) =>
    pendingAttachmentsByScope[scope] ?? [];

  const attachmentsForScope = (scope) => {
    if (scope !== EPHEMERAL_ATTACHMENT_SCOPE) hydratePendingAttachments(scope);
    return cachedAttachmentsForScope(scope);
  };

  const nextAttachmentLocalId = () => {
    nextPendingAttachmentId += 1;
    return `pending-attachment-${nextPendingAttachmentId}`;
  };

  const hydratePendingAttachments = (key) => {
    const scope = attachmentScopeForDraftKey(key);
    if (!key) return;
    const current = cachedAttachmentsForScope(scope);
    const stored = Object.fromEntries(
      getPendingAttachments(key).map((attachment) => [
        attachment.attachment_id,
        attachment,
      ]),
    );
    const restored = current.filter((attachment) => {
      if (!attachment.attachment_id) return true;
      if (!Object.hasOwn(stored, attachment.attachment_id)) {
        safeRevokeObjectUrl(attachment.preview_url);
        return false;
      }
      const saved = stored[attachment.attachment_id];
      // Keep identity: an admitted send removes its captured attachments by
      // reference, and a still-mounted preview keeps its local object URL.
      attachment.filename = saved.filename;
      attachment.media_type = saved.media_type;
      delete stored[attachment.attachment_id];
      return true;
    });
    for (const attachment of Object.values(stored)) {
      restored.push({
        ...attachment,
        local_id: nextAttachmentLocalId(),
        preview_url: getAttachmentUrl(
          encodeURIComponent(attachment.attachment_id),
        ),
        uploading: false,
      });
    }
    if (
      scope in pendingAttachmentsByScope &&
      restored.length === current.length &&
      restored.every((attachment, index) => attachment === current[index])
    )
      return;
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
    // Reconcile synchronously before writing, even when an upload settles
    // before the next render after another tab removed a completed file.
    const scope = attachmentScopeForDraftKey(key);
    const current = attachmentsForScope(scope);
    setAttachmentsForDraftKey(key, update(current));
  };

  const unsubscribeAttachments = subscribePendingAttachments((change) => {
    if (change?.mediaKey) {
      if (!destroyed && change.mediaKey === context.draftKey)
        restoreMedia(change.mediaKey);
      return;
    }
    if (change?.fromAgent) {
      const prefix = `${change.fromAgent}::`;
      pendingAttachmentsByScope = Object.fromEntries(
        Object.entries(pendingAttachmentsByScope).map(
          ([scope, attachments]) => [
            scope.startsWith(prefix)
              ? `${change.toAgent}::${scope.slice(prefix.length)}`
              : scope,
            attachments,
          ],
        ),
      );
    }
    for (const scope of Object.keys(pendingAttachmentsByScope)) {
      if (scope !== EPHEMERAL_ATTACHMENT_SCOPE)
        hydratePendingAttachments(scope);
    }
  });

  // An attachment can move to another draft while it uploads (a first send
  // creates the Session its draft continues in); its upload follows it there.
  const draftKeyHoldingAttachment = (localId, fallbackKey) => {
    for (const [scope, attachments] of Object.entries(
      pendingAttachmentsByScope,
    )) {
      if (attachments.some((attachment) => attachment.local_id === localId)) {
        return scope === EPHEMERAL_ATTACHMENT_SCOPE ? '' : scope;
      }
    }
    return fallbackKey;
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

  const showTranscriptionError = (message) => {
    const normalizedMessage =
      typeof message === 'string' && message.length > 0
        ? message
        : t('chat.voice.transcriptionFailed');
    showComposerErrorToast(normalizedMessage);
    context.onTranscriptionError?.(normalizedMessage);
  };

  const removePendingAttachmentByLocalId = (key, localId) => {
    const upload = uploads.get(localId);
    if (upload) {
      uploads.delete(localId);
      upload.abort.abort();
      upload.transfer.cancel();
      clearTimeout(upload.timer);
    }
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

  // Named by the moment of the paste on the Settings timezone's clock.
  const buildPastedImageFileName = () => {
    const parts = dateTimePartsInZone(Date.now(), dateTimePrefs.timeZone);
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    const time = `${parts.hour}-${parts.minute}-${parts.second}`;
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

    const uploadTasks = selectedFiles.map((file, index) => {
      const localId = newAttachments[index].local_id;
      uploads.set(localId, {
        file,
        key: attachmentDraftKey,
        transfer: createMediaTransfer('attachment'),
        abort: new AbortController(),
        retryAvailable: false,
        retried: false,
      });
      return uploadFile(localId);
    });
    await Promise.all(uploadTasks);
  };

  async function uploadFile(localId, manual = false) {
    const upload = uploads.get(localId);
    if (!upload || upload.busy) return;
    if (manual && upload.renew) {
      upload.transfer.cancel();
      upload.transfer = createMediaTransfer('attachment');
      upload.renew = false;
    }
    clearTimeout(upload.timer);
    upload.busy = true;
    upload.retryAvailable = false;
    const update = (changes) => {
      const key = draftKeyHoldingAttachment(localId, upload.key);
      updateAttachmentsForDraftKey(key, (attachments) =>
        attachments.map((attachment) =>
          attachment.local_id === localId
            ? { ...attachment, ...changes }
            : attachment,
        ),
      );
    };
    update({ uploading: true, retryAvailable: false });
    try {
      const result = await uploadAttachment(upload.file, {
        transfer: upload.transfer,
        signal: upload.abort.signal,
      });
      if (
        !uploads.delete(localId) ||
        ownerGeneration !== currentMediaGeneration()
      )
        return;
      update({
        attachment_id: result.attachment_id,
        filename: result.filename,
        media_type: result.media_type,
        uploading: false,
      });
    } catch (error) {
      if (!uploads.has(localId)) return;
      upload.retryAvailable = interruptedTransfer(error);
      upload.renew = !upload.retryAvailable;
      upload.error = error.message || t('chat.attachment.uploadFailed');
      if (!destroyed) {
        update({ uploading: false, retryAvailable: true, error: upload.error });
        if (upload.retryAvailable && !upload.retried) {
          upload.retried = true;
          upload.timer = setTimeout(() => void uploadFile(localId), 1000);
        }
        return;
      }
      parkUpload(localId, upload);
    } finally {
      upload.busy = false;
    }
  }

  function parkUpload(id, upload) {
    clearTimeout(upload.timer);
    uploads.delete(id);
    const key = draftKeyHoldingAttachment(id, upload.key);
    const index = cachedAttachmentsForScope(
      attachmentScopeForDraftKey(key),
    ).findIndex((card) => card.local_id === id);
    parkDraftMedia(
      key,
      { kind: 'file', upload, index, transfer: upload.transfer },
      ownerGeneration,
    );
  }

  function restoreMedia(key) {
    if (!key || destroyed) return;
    for (const entry of takeDraftMedia(
      key,
      !pendingRecording && recordingState === 'idle',
    )) {
      if (entry.kind === 'recording') {
        pendingRecording = entry.recording;
        retryRecording = true;
        recordingOutcomeUnknown = !!pendingRecording.unknown;
        if (!pendingRecording.renew)
          queueMicrotask(() => void transcribeRecording());
        continue;
      }
      const upload = entry.upload;
      const id = nextAttachmentLocalId();
      upload.key = key;
      uploads.set(id, upload);
      const file = upload.file;
      const card = {
        local_id: id,
        attachment_id: '',
        filename: file.name || 'upload.bin',
        media_type: file.type || 'application/octet-stream',
        preview_url: globalThis.URL?.createObjectURL?.(file) ?? '',
        uploading: false,
        retryAvailable: true,
        error: upload.error,
      };
      updateAttachmentsForDraftKey(key, (cards) => {
        const next = [...cards];
        next.splice(entry.index < 0 ? next.length : entry.index, 0, card);
        return next;
      });
      if (upload.retryAvailable) queueMicrotask(() => void uploadFile(id));
    }
  }

  function leaveDraft(key) {
    if (pendingRecording && key) {
      const recording = pendingRecording;
      clearTimeout(recording.timer);
      pendingRecording = null;
      parkDraftMedia(
        key,
        { kind: 'recording', recording, transfer: recording.transfer },
        ownerGeneration,
      );
    }
    cancelActiveRecording();
  }

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
      // A local speech model loads while the user speaks. Only a hint: the
      // transcription reports its own failures.
      prepareSpeechTranscription().catch(() => {});
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
        `${t('chat.voice.startFailed')} ${error.message ?? ''}`.trim(),
      );
    }
  };

  const stopRecordingAndTranscribe = async () => {
    const recorder = activeRecorder;
    if (!recorder) {
      recordingState = 'idle';
      return;
    }

    recordingState = 'transcribing';
    transcriptionProgress = { phase: 'uploading', elapsed_seconds: 0 };
    const requestGeneration = ++recorderRequestGeneration;
    try {
      const audioBlob = await recorder.stop();
      if (destroyed || requestGeneration !== recorderRequestGeneration) return;
      activeRecorder = null;
      pendingRecording = {
        blob: audioBlob,
        filename: recorder.filename(),
        transfer: createMediaTransfer('transcription'),
        retried: false,
      };
      await transcribeRecording();
    } catch (error) {
      if (destroyed || requestGeneration !== recorderRequestGeneration) return;
      cancelActiveRecording();
      showTranscriptionError(
        `${t('chat.voice.transcriptionFailed')} ${error.message ?? ''}`.trim(),
      );
    }
  };

  async function transcribeRecording(manual = false) {
    const recording = pendingRecording;
    if (!recording || transcriptionAbort) return;
    if (manual && recording.renew) {
      recording.transfer.cancel();
      recording.transfer = createMediaTransfer('transcription');
      recording.renew = false;
    }
    clearTimeout(recording.timer);
    retryRecording = false;
    recordingState = 'transcribing';
    transcriptionProgress = { phase: 'uploading', elapsed_seconds: 0 };
    const requestGeneration = recorderRequestGeneration;
    const abort = new AbortController();
    transcriptionAbort = abort;
    try {
      const result = await transcribeSpeech(recording.blob, {
        transfer: recording.transfer,
        signal: abort.signal,
        filename: recording.filename,
        onProgress: (progress) => {
          if (!destroyed && requestGeneration === recorderRequestGeneration)
            transcriptionProgress = progress;
        },
      });
      if (destroyed || requestGeneration !== recorderRequestGeneration) return;
      pendingRecording = null;
      await context.insertTranscript(result.text);
    } catch (error) {
      if (destroyed || requestGeneration !== recorderRequestGeneration) return;
      if (pendingRecording) {
        retryRecording = true;
        recordingOutcomeUnknown = error?.status === 410;
        recording.unknown = recordingOutcomeUnknown;
        recording.renew = !interruptedTransfer(error);
        if (!recording.renew && !recording.retried) {
          recording.retried = true;
          recording.timer = setTimeout(() => void transcribeRecording(), 1000);
        }
      } else {
        recording.transfer.cancel();
        pendingRecording = null;
      }
      showTranscriptionError(
        `${t('chat.voice.transcriptionFailed')} ${error.message ?? ''}`.trim(),
      );
    } finally {
      if (requestGeneration === recorderRequestGeneration) {
        transcriptionAbort = null;
        recordingState = 'idle';
        if (!pendingRecording) restoreMedia(context.draftKey);
      }
    }
  }

  const cancelActiveRecording = () => {
    recorderRequestGeneration += 1;
    clearTimeout(pendingRecording?.timer);
    pendingRecording?.transfer.cancel();
    pendingRecording = null;
    retryRecording = false;
    transcriptionAbort?.abort();
    transcriptionAbort = null;
    recordingState = 'idle';
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

  const unsubscribeReady = onServerReady(() => {
    for (const [localId, upload] of uploads)
      if (upload.retryAvailable) void uploadFile(localId);
    if (retryRecording && !pendingRecording?.renew) void transcribeRecording();
  });

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
    unsubscribeReady();
    for (const [id, upload] of uploads) {
      clearTimeout(upload.timer);
      // A still-sending upload can publish its id into its keyed draft after
      // a component remount. An abandoned retry has no mounted owner.
      if (!upload.busy) parkUpload(id, upload);
      else if (!upload.key) removePendingAttachmentByLocalId('', id);
    }
    unsubscribeAttachments();
    if (attachmentToastTimeoutId !== null) {
      clearTimeout(attachmentToastTimeoutId);
      attachmentToastTimeoutId = null;
    }
    leaveDraft(context.draftKey);
    releasePendingAttachmentPreviews();
  }
  return {
    destroy,
    leaveDraft,
    restoreMedia,
    uploadFile,
    transcribeRecording,
    get retryRecording() {
      return retryRecording;
    },
    get recordingOutcomeUnknown() {
      return recordingOutcomeUnknown;
    },
    discardRecording() {
      cancelActiveRecording();
      restoreMedia(context.draftKey);
    },
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
