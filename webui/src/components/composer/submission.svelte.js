import {
  clearDraft,
  getDraft,
  pushHistory,
  setDraft,
} from '$lib/composerMemory.js';
import {
  extractMentionTokens,
  resolveMentionFiles,
} from '$lib/fileMentions.js';

// Snapshot a submission before asynchronous lookups, then consume only its admitted draft.
export function createComposerSubmission(context) {
  const { media, picker } = context;
  let inFlight = $state(false);

  // Which @-tokens in the outgoing text are actual files. Decided against the
  // picker's file list (fetched now if this draft never opened the picker, e.g.
  // a restored draft), then against the entries of a token's folder, so files
  // reached by browsing count too while pasted code decorators and handles
  // never expand.
  const collectFileMentions = async (snapshot) => {
    let files = snapshot.fileCandidates;
    if (files === null && typeof snapshot.listFiles === 'function') {
      try {
        const result = await snapshot.listFiles();
        files = Array.isArray(result?.files) ? result.files : [];
        if (context.draftKey === snapshot.draftKey) {
          picker.fileCandidates = files;
          picker.fileDirectories = Array.isArray(result?.directories)
            ? result.directories
            : [];
          picker.fileListTruncated = Boolean(result?.truncated);
        }
      } catch {
        files = [];
      }
    }
    const listEntries = async (directory) => {
      const listed = snapshot.listedEntries?.(directory);
      if (listed) {
        return listed;
      }
      if (typeof snapshot.listFiles !== 'function') {
        return [];
      }
      const result = await snapshot.listFiles({ directory });
      return Array.isArray(result?.entries) ? result.entries : [];
    };
    return resolveMentionFiles(snapshot.mentionTokens, {
      files: files ?? [],
      listEntries,
    });
  };

  const createSubmitSnapshot = () => ({
    content: context.content,
    trimmedContent: context.content.trim(),
    inputOrigin: context.inputOrigin,
    draftKey: context.draftKey,
    historyKey: context.historyKey,
    mentionTokens: extractMentionTokens(context.content),
    fileCandidates:
      picker.fileCandidates === null ? null : Array.from(picker.fileCandidates),
    listedEntries: picker.listedEntries(),
    listFiles: context.onListFiles,
    sendMessage: context.onSendMessage,
    attachments: media.pendingAttachments.map((attachment) => ({
      source: attachment,
      attachment_id: attachment.attachment_id,
      filename: attachment.filename,
      media_type: attachment.media_type,
    })),
  });

  const submitBlocked = (snapshot) =>
    context.disabled ||
    inFlight ||
    media.hasUploadingAttachments ||
    media.voiceBusy ||
    media.isRecording ||
    (!snapshot.trimmedContent && snapshot.attachments.length === 0);

  const submit = async (snapshot = createSubmitSnapshot()) => {
    if (submitBlocked(snapshot)) {
      return;
    }

    inFlight = true;
    try {
      const fileMentions =
        snapshot.mentionTokens.length === 0
          ? []
          : await collectFileMentions(snapshot);
      await finishSubmit(snapshot, fileMentions);
    } finally {
      inFlight = false;
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

    let result;
    try {
      result = sendOptions
        ? await snapshot.sendMessage(outgoingContent, sendOptions)
        : await snapshot.sendMessage(outgoingContent);
    } catch {
      result = false;
    }
    const continuedDraftKey =
      typeof result?.draftKey === 'string' ? result.draftKey : '';
    if (result !== true && !continuedDraftKey) {
      return false;
    }

    // Only successful admission makes this a sent-history entry. A failed RPC
    // leaves both the draft and its recall history untouched.
    pushHistory(snapshot.historyKey, snapshot.content);

    const submittedAttachmentSources = snapshot.attachments.map(
      (attachment) => attachment.source,
    );
    const submittedAttachmentsStillPresent = media
      .attachmentsForScope(media.attachmentScopeForDraftKey(snapshot.draftKey))
      .filter((attachment) => submittedAttachmentSources.includes(attachment));
    for (const attachment of submittedAttachmentsStillPresent) {
      media.safeRevokeObjectUrl(attachment.preview_url);
    }
    media.updateAttachmentsForDraftKey(snapshot.draftKey, (attachments) =>
      attachments.filter(
        (attachment) => !submittedAttachmentSources.includes(attachment),
      ),
    );

    // Typing and navigation stay available while admission is pending. Clear
    // only the exact draft snapshot that succeeded; later edits or another
    // Session's draft must survive the older request completing.
    if (getDraft(snapshot.draftKey) === snapshot.content) {
      clearDraft(snapshot.draftKey);
    }
    if (
      context.draftKey === snapshot.draftKey &&
      context.content === snapshot.content
    ) {
      context.content = '';
      context.inputOrigin = '';
      picker.triggerContext = null;
      picker.activeSkillIndex = 0;
      media.isDragOver = false;
      context.resetInputHistory();
    }
    if (continuedDraftKey && continuedDraftKey !== snapshot.draftKey) {
      moveDraft(snapshot.draftKey, continuedDraftKey);
    }
    return true;
  };

  // What a draft holds (text, attachments, uploads still running) moves to
  // `toKey`. A send that created the draft's Session moves what was added
  // meanwhile, as it would have stayed in a Session's composer, unless the
  // Session already holds its own. A draft moved to another Agent (`merge`)
  // adds its text and attachments after what that Agent's draft holds.
  const moveDraft = (fromKey, toKey, { merge = false } = {}) => {
    if (!fromKey || !toKey || fromKey === toKey) {
      return;
    }
    const movedText = getDraft(fromKey);
    const keptText = getDraft(toKey);
    if (movedText && (merge || !keptText)) {
      setDraft(
        toKey,
        keptText
          ? `${keptText}

${movedText}`
          : movedText,
      );
      clearDraft(fromKey);
    }
    const fromScope = media.attachmentScopeForDraftKey(fromKey);
    const movedAttachments = media.attachmentsForScope(fromScope);
    if (
      movedAttachments.length > 0 &&
      (merge ||
        media.attachmentsForScope(media.attachmentScopeForDraftKey(toKey))
          .length === 0)
    ) {
      media.updateAttachmentsForDraftKey(toKey, (attachments) => [
        ...attachments,
        ...movedAttachments,
      ]);
      media.updateAttachmentsForDraftKey(fromKey, (attachments) =>
        attachments.filter(
          (attachment) => !movedAttachments.includes(attachment),
        ),
      );
    }
    // A composer already showing the Session shows what moved there; one
    // that has not switched yet loads it when it does.
    context.refreshMovedDraft(toKey);
  };

  function submitCommand(command) {
    const snapshot = {
      ...createSubmitSnapshot(),
      content: command,
      trimmedContent: command,
      inputOrigin: '',
      mentionTokens: [],
      attachments: [],
    };
    if (submitBlocked(snapshot)) return;
    context.content = command;
    setDraft(context.draftKey, command);
    context.inputOrigin = '';
    picker.triggerContext = null;
    picker.activeSkillIndex = 0;
    picker._triggerClosed = true;
    media.isDragOver = false;
    context.resetInputHistory();
    void submit(snapshot);
  }

  return {
    get inFlight() {
      return inFlight;
    },
    submit,
    submitCommand,
    moveDraft,
  };
}
