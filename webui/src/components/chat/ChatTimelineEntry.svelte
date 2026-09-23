<script>
  import { toolDetailImages } from '$lib/chatToolDetails.js';
  import { t } from '$lib/i18n.js';
  import { formatMentionToken } from '$lib/fileMentions.js';
  import { isImeComposing } from '$lib/keyboard.js';
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';
  import { linkifiedTextSegments } from '$lib/markdown.js';
  import {
    attachmentFilename,
    attachmentPreviewLabel,
    attachmentUrlForBlock,
    fileMentionStatusLabel,
    avatarForItem,
    errorMessagePresentation,
    formatTime,
    hasAssistantContent,
    hasReadableReasoning,
    hasUserContentBlocks,
    isAssistantItem,
    isFailedToolEvent,
    isFileContentBlock,
    isFileMentionContentBlock,
    isImageMediaContentBlock,
    imageReferenceLabel,
    isMediaContentBlock,
    isReasoningOnlyAssistantMessage,
    isTerminalEvent,
    isTextContentBlock,
    isTextToSpeechResult,
    isToolEvent,
    isUserItem,
    labelForEvent,
    labelForMessage,
    messageFromEvent,
    metaForEvent,
    reasoningDurationLabel,
    shouldRenderMessage,
    speechArtifactFromResult,
    takeoverSeparatorLabel,
    textFromEvent,
    textFromMessage,
    toolCallFromEvent,
    toolDetailPresentation,
    toolNameForEvent,
    toolResultValueForEvent,
    toolRowFromEvent,
    toolRowPresentation,
    toolStatus,
    userContentBlocks,
  } from '$lib/chatTimelinePresentation.js';

  import CopyButton from '../ui/CopyButton.svelte';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import Button from '../ui/Button.svelte';
  import TextArea from '../ui/TextArea.svelte';
  import ChatCompactionSeparator from './ChatCompactionSeparator.svelte';
  import MarkdownContent from './MarkdownContent.svelte';
  import ChatReasoning from './ChatReasoning.svelte';
  import ToolPrimaryLine from './ToolPrimaryLine.svelte';

  let {
    item,
    agentName = '',
    isReasoningOpen = () => false,
    onReasoningOpenChange = () => {},
    messageEditingDisabled = false,
    onEditMessage = async () => false,
  } = $props();

  let editing = $state(false);
  let editedContent = $state('');
  let editSaving = $state(false);

  function beginEditing(message) {
    editedContent = typeof message?.content === 'string' ? message.content : '';
    editing = true;
  }

  function cancelEditing(force = false) {
    if (editSaving && !force) {
      return;
    }
    editing = false;
    editedContent = '';
  }

  async function submitEdit(message) {
    if (editSaving || editedContent.trim().length === 0) {
      return;
    }
    editSaving = true;
    try {
      const accepted = await onEditMessage(message.id, editedContent);
      if (accepted) {
        cancelEditing(true);
      }
    } finally {
      editSaving = false;
    }
  }

  function handleEditKeydown(event, message) {
    if (isImeComposing(event)) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelEditing();
      return;
    }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void submitEdit(message);
    }
  }

  function copyableMessageText(message) {
    if (typeof message?.content === 'string') {
      return message.content;
    }
    if (message?.role !== 'user') {
      return '';
    }

    return userContentBlocks(message)
      .flatMap((block) => {
        if (isTextContentBlock(block)) {
          return [block.text];
        }
        if (isFileMentionContentBlock(block)) {
          return [formatMentionToken(block.path)];
        }
        return [];
      })
      .join('\n\n');
  }

  function copyLabelForMessage(message) {
    return message?.role === 'assistant'
      ? t('chat.copyAnswer', 'Copy answer')
      : t('chat.copyUserMessage', 'Copy message');
  }

  function copiedLabelForMessage(message) {
    return message?.role === 'assistant'
      ? t('chat.answerCopied', 'Answer copied')
      : t('chat.userMessageCopied', 'Message copied');
  }
</script>

{#snippet toolDetailSection(
  label,
  value,
  isError = false,
  preferPayload = false,
  toolName = '',
  tool = null,
)}
  {@const images = toolDetailImages(value, { preferPayload, tool })}
  {@const presentation = toolDetailPresentation(value, {
    preferPayload,
    toolName,
    tool,
  })}
  <div
    class="teb-row teb-section"
    class:teb-section--error={isError}
    class:teb-section--success={preferPayload && !isError}
  >
    <div class="teb-section-header">
      <span class="teb-label">{label}</span>
      {#if presentation.copyText !== t('chat.toolNoData', '—')}
        <CopyButton
          text={presentation.copyText}
          class="chat-copy-action tool-detail-copy"
          label={t('chat.copyToolField', 'Copy {label}', { label })}
        />
      {/if}
    </div>
    {#if images.length > 0}
      <div class="tool-image-previews">
        {#each images as image (image.src)}
          <a
            class="tool-image-preview"
            href={image.src}
            target="_blank"
            rel="noreferrer"
            aria-label={image.filename}
          >
            <img
              src={image.src}
              alt={image.filename}
              loading="lazy"
              onerror={(event) => {
                event.currentTarget.hidden = true;
              }}
            />
            <span
              class="image-unavailable"
              role="img"
              aria-label={t('chat.image.unavailable', 'Image not available')}
            >
              <svg viewBox="0 0 32 24" aria-hidden="true"
                ><rect x="1" y="1" width="30" height="22" rx="2" /><circle
                  cx="10"
                  cy="8"
                  r="2"
                /><path d="m3 20 8-8 6 6 4-4 8 6M3 2l26 20" /></svg
              >
              <span>{t('chat.image.unavailable', 'Image not available')}</span>
            </span>
            <span>{image.filename}</span>
          </a>
        {/each}
      </div>
    {/if}
    {#if presentation.kind === 'fields'}
      <div class:error={isError} class="teb-code teb-fields">
        {#each presentation.fields as field (field.key)}
          <div class="teb-field">
            <span class="teb-field-key">{field.key}</span>
            <span
              class:error={isError}
              class={`teb-field-value teb-field-value--${field.kind}`}
              >{field.text}</span
            >
          </div>
        {/each}
      </div>
    {:else}
      <span
        class:error={isError}
        class={`teb-code teb-text teb-text--${presentation.kind}`}
        >{presentation.text}</span
      >
    {/if}
  </div>
{/snippet}

{#snippet linkifiedText(text)}
  {#each linkifiedTextSegments(text) as segment, segmentIndex (segmentIndex)}
    {#if segment.href}
      <a href={segment.href} target="_blank" rel="noopener noreferrer"
        >{segment.text}</a
      >
    {:else}
      {segment.text}
    {/if}
  {/each}
{/snippet}

{#snippet userContentBlock(block)}
  {#if isTextContentBlock(block)}
    <p class="msg-body-text msg-body-text--user">
      {@render linkifiedText(block.text)}
    </p>
  {:else if isImageMediaContentBlock(block)}
    {@const mediaUrl = attachmentUrlForBlock(block)}
    {#if mediaUrl}
      <div class="attachment-item attachment-item-image inline-attachment-card">
        <!-- The enlarged preview is a decorative pointer/keyboard aid; a tap
             follows the link instead of opening it. -->
        <a
          class="inline-attachment"
          href={mediaUrl}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={attachmentPreviewLabel(block)}
        >
          <img
            class="attachment-thumb"
            src={mediaUrl}
            alt={attachmentPreviewLabel(block)}
            loading="lazy"
          />
          <span
            class="floating-card attachment-hover-preview"
            aria-hidden="true"
            use:floatingHoverCard={{ accessible: false, touch: false }}
          >
            <img class="attachment-hover-image" src={mediaUrl} alt="" />
          </span>
        </a>
        <div class="attachment-meta">
          <span class="attachment-name" use:tooltip={attachmentFilename(block)}
            >{imageReferenceLabel(block)}</span
          >
        </div>
      </div>
    {/if}
  {:else if isFileContentBlock(block) || isMediaContentBlock(block)}
    {@const fileUrl = attachmentUrlForBlock(block)}
    <div class="inline-file">
      <svg
        class="inline-file-icon"
        viewBox="0 0 16 16"
        width="14"
        height="14"
        aria-hidden="true"
      >
        <path
          d="M3.5 1.5h6.5l2.5 2.5v10.5H3.5z"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
        />
        <path
          d="M10 1.5V4h2.5"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
        />
      </svg>
      {#if fileUrl}
        <a
          class="inline-file-link"
          href={fileUrl}
          download={attachmentFilename(block)}
          use:tooltip={attachmentFilename(block)}
        >
          {attachmentFilename(block)}
        </a>
      {:else}
        <span class="inline-file-name">{attachmentFilename(block)}</span>
      {/if}
    </div>
  {:else if isFileMentionContentBlock(block)}
    {@const statusLabel = fileMentionStatusLabel(block)}
    <div
      class="inline-file"
      use:tooltip={t('chat.fileMention.label', 'Mentioned file')}
    >
      <svg
        class="inline-file-icon"
        viewBox="0 0 16 16"
        width="14"
        height="14"
        aria-hidden="true"
      >
        <path
          d="M3.5 1.5h6.5l2.5 2.5v10.5H3.5z"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
        />
        <path
          d="M10 1.5V4h2.5"
          fill="none"
          stroke="currentColor"
          stroke-width="1.2"
        />
      </svg>
      <span class="inline-file-name" use:tooltip={block.path}
        >@{block.path}</span
      >
      {#if statusLabel}
        <span class="inline-file-status">({statusLabel})</span>
      {/if}
    </div>
  {/if}
{/snippet}

{#if item.type === 'message' && shouldRenderMessage(item.message)}
  <article
    class:assistant={item.message.role === 'assistant'}
    class:user={item.message.role === 'user'}
    class:error={item.message.role === 'error'}
    data-run-id={item.message.run_id ?? ''}
    class="msg"
  >
    <div class="msg-header">
      <div class="msg-avatar">{avatarForItem(item)}</div>
      <span class="msg-author"
        >{item.message.role === 'assistant'
          ? agentName || labelForMessage(item.message)
          : labelForMessage(item.message)}</span
      >
      {#if formatTime(item.message.timestamp)}
        <span class="msg-timestamp">{formatTime(item.message.timestamp)}</span>
      {/if}
      {#if ['assistant', 'user'].includes(item.message.role) && copyableMessageText(item.message)}
        <CopyButton
          text={copyableMessageText(item.message)}
          class="chat-copy-action message-copy"
          label={copyLabelForMessage(item.message)}
          copiedLabel={copiedLabelForMessage(item.message)}
        />
      {/if}
      {#if item.message.editable === true && !editing}
        <Button
          variant="tertiary"
          icon
          class="chat-edit-action message-edit"
          ariaLabel={t('chat.editMessage', 'Edit message')}
          tooltip={t('chat.editMessage', 'Edit message')}
          disabled={messageEditingDisabled}
          onClick={() => beginEditing(item.message)}
        >
          <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
            <path d="M4 20h4l11-11-4-4L4 16v4ZM13.5 6.5l4 4" />
          </svg>
        </Button>
      {/if}
    </div>
    <div class="msg-content">
      {#if editing}
        <div class="message-edit-form">
          <TextArea
            value={editedContent}
            onInput={(value) => {
              editedContent = value;
            }}
            rows={3}
            autofocus
            disabled={editSaving}
            ariaLabel={t('chat.editMessageInput', 'Edit message text')}
            onkeydown={(event) => handleEditKeydown(event, item.message)}
          />
          <div class="message-edit-actions">
            <Button
              variant="tertiary"
              disabled={editSaving}
              onClick={cancelEditing}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              variant="primary"
              loading={editSaving}
              disabled={editedContent.trim().length === 0}
              onClick={() => submitEdit(item.message)}
            >
              {t('chat.saveAndRestart', 'Save & restart')}
            </Button>
          </div>
          <span class="message-edit-hint">
            {t(
              'chat.editRestartHint',
              'Later messages will be removed from the active conversation.',
            )}
          </span>
        </div>
      {:else}
        {#if hasReadableReasoning(item.message) && (hasAssistantContent(item.message) || item.message.reasoning_summary?.length)}
          <ChatReasoning
            source={item.message.reasoning ?? ''}
            summary={item.message.reasoning_summary}
            open={isReasoningOpen(item.id)}
            durationLabel={reasoningDurationLabel({
              durationMs: item.message.reasoning_timing?.duration_ms,
            })}
            onOpenChange={(open) => onReasoningOpenChange(item.id, open)}
          />
        {/if}
        {#if hasUserContentBlocks(item.message)}
          <div class="msg-body-blocks">
            {#each userContentBlocks(item.message) as block, blockIndex (`${item.id}-block-${blockIndex}`)}
              {@render userContentBlock(block)}
            {/each}
          </div>
        {:else if textFromMessage(item.message)}
          {#if item.message.role === 'assistant'}
            {#if isReasoningOnlyAssistantMessage(item.message)}
              {#if !item.message.reasoning_summary?.length}<p
                  class="msg-body-text"
                >
                  {textFromMessage(item.message)}
                </p>{/if}
            {:else}
              <MarkdownContent
                source={textFromMessage(item.message)}
                class="msg-markdown"
              />
            {/if}
          {:else if item.message.role === 'error'}
            {@const errorPresentation = errorMessagePresentation(
              textFromMessage(item.message),
            )}
            <p class="msg-body-text">{errorPresentation.summary}</p>
            {#if errorPresentation.details}
              <details class="error-details">
                <summary class="error-details-summary">
                  {t('chat.errorDetails', 'Details')}
                </summary>
                <pre
                  class="error-details-body">{errorPresentation.details}</pre>
              </details>
            {/if}
          {:else}
            <p
              class="msg-body-text"
              class:msg-body-text--user={item.message.role === 'user'}
            >
              {@render linkifiedText(textFromMessage(item.message))}
            </p>
          {/if}
        {/if}
      {/if}
    </div>
  </article>
{:else if item.type === 'compaction_separator'}
  <ChatCompactionSeparator {item} />
{:else if item.type === 'takeover_separator'}
  <div class="date-sep takeover-sep">
    {takeoverSeparatorLabel(item.message)}
  </div>
{:else if item.type === 'event'}
  {#if isToolEvent(item.event)}
    {@const eventToolRow = toolRowFromEvent(item.event)}
    {@const eventPresentation = toolRowPresentation(eventToolRow)}
    <article class="msg assistant">
      <div class="msg-header">
        <div class="msg-avatar">{avatarForItem(item)}</div>
        <span class="msg-author">{labelForEvent(item.event)}</span>
        {#if formatTime(item.event.timestamp)}
          <span class="msg-timestamp">{formatTime(item.event.timestamp)}</span>
        {/if}
        {#if (isAssistantItem(item) || isUserItem(item)) && (copyableMessageText(messageFromEvent(item.event)) || textFromEvent(item.event))}
          <CopyButton
            text={copyableMessageText(messageFromEvent(item.event)) ||
              textFromEvent(item.event)}
            class="chat-copy-action message-copy"
            label={copyLabelForMessage(messageFromEvent(item.event))}
            copiedLabel={copiedLabelForMessage(messageFromEvent(item.event))}
          />
        {/if}
      </div>
      <div class="msg-content">
        <details class="tool-event">
          <summary class="tool-event-line">
            <span
              class:error={toolStatus(eventToolRow) === 'failed'}
              class:partial={toolStatus(eventToolRow) === 'partial'}
              class:running={toolStatus(eventToolRow) === 'running'}
              class:done={toolStatus(eventToolRow) === 'success'}
              class="te-dot">●</span
            >
            <span class="te-fn">{toolNameForEvent(item.event)}</span>
            {#if eventPresentation.primary.length > 0}
              <ToolPrimaryLine primary={eventPresentation.primary} />
            {/if}
            {#each eventPresentation.facts as fact, index (`${fact.kind}:${index}`)}
              <span
                class="te-fact"
                class:te-fact--added={fact.variant === 'added'}
                class:te-fact--removed={fact.variant === 'removed'}
                >{fact.text}</span
              >
            {/each}
          </summary>
          <div class="tool-event-body tool-event-details">
            {@render toolDetailSection(
              t('chat.toolArgs', 'Args'),
              toolCallFromEvent(item.event)?.arguments,
              false,
              false,
              toolNameForEvent(item.event),
              eventToolRow,
            )}
            {#if toolResultValueForEvent(item.event)}
              {@render toolDetailSection(
                t('chat.toolResultLabel', 'Result'),
                toolResultValueForEvent(item.event),
                isFailedToolEvent(item.event),
                true,
                toolNameForEvent(item.event),
                eventToolRow,
              )}
            {/if}
          </div>
        </details>
        {#if isTextToSpeechResult(item.event)}
          {@const speechArtifact = speechArtifactFromResult(item.event)}
          {#if speechArtifact}
            <AudioPlayer
              class="speech-audio-player"
              src={speechArtifact.url}
              autoplay
            />
          {/if}
        {/if}
      </div>
    </article>
  {:else if isTerminalEvent(item.event)}
    <p class="chat-terminal-event">
      <span>{labelForEvent(item.event)}</span>
      {#if metaForEvent(item.event)}
        <span>· {metaForEvent(item.event)}</span>
      {/if}
    </p>
  {:else if textFromEvent(item.event) || hasUserContentBlocks(messageFromEvent(item.event))}
    <article
      class:assistant={isAssistantItem(item)}
      class:user={isUserItem(item)}
      data-run-id={item.event.run_id ?? ''}
      class="msg"
    >
      <div class="msg-header">
        <div class="msg-avatar">{avatarForItem(item)}</div>
        <span class="msg-author">{labelForEvent(item.event)}</span>
        {#if formatTime(item.event.timestamp)}
          <span class="msg-timestamp">{formatTime(item.event.timestamp)}</span>
        {/if}
      </div>
      <div class="msg-content">
        {#if item.event.type === 'reasoning'}
          <ChatReasoning
            source={textFromEvent(item.event)}
            summary={messageFromEvent(item.event)?.reasoning_summary}
            open={isReasoningOpen(item.id)}
            durationLabel={reasoningDurationLabel({
              durationMs: messageFromEvent(item.event)?.reasoning_timing
                ?.duration_ms,
            })}
            onOpenChange={(open) => onReasoningOpenChange(item.id, open)}
          />
        {:else if hasUserContentBlocks(messageFromEvent(item.event))}
          <div class="msg-body-blocks">
            {#each userContentBlocks(messageFromEvent(item.event)) as block, blockIndex (`${item.id}-block-${blockIndex}`)}
              {@render userContentBlock(block)}
            {/each}
          </div>
        {:else}
          <p class="msg-body-text" class:msg-body-text--user={isUserItem(item)}>
            {@render linkifiedText(textFromEvent(item.event))}
          </p>
        {/if}
      </div>
    </article>
  {/if}
{/if}
