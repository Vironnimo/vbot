<script>
  import { t } from '$lib/i18n.js';
  import { floatingHoverCard, tooltip } from '$lib/tooltip.js';
  import {
    linkifiedTextSegments,
    reasoningMarkdownSource,
  } from '$lib/markdown.js';
  import {
    attachmentFilename,
    attachmentPreviewLabel,
    attachmentUrlForBlock,
    fileMentionStatusLabel,
    avatarForItem,
    compactToolValue,
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
    isMediaContentBlock,
    isReasoningOnlyAssistantMessage,
    isRunningToolEvent,
    isTerminalEvent,
    isTextContentBlock,
    isTextToSpeechResult,
    isToolEvent,
    isUserItem,
    labelForEvent,
    labelForMessage,
    messageFromEvent,
    metaForEvent,
    shouldRenderMessage,
    speechArtifactFromResult,
    takeoverSeparatorLabel,
    textFromEvent,
    textFromMessage,
    toolArgumentForEvent,
    toolCallFromEvent,
    toolNameForEvent,
    toolResultValueForEvent,
    toolRowFromEvent,
    userContentBlocks,
  } from '$lib/chatTimelinePresentation.js';

  import CopyButton from '../ui/CopyButton.svelte';
  import ChatCompactionSeparator from './ChatCompactionSeparator.svelte';
  import MarkdownContent from './MarkdownContent.svelte';

  let {
    item,
    agentName = '',
    isReasoningOpen = () => false,
    onReasoningOpenChange = () => {},
  } = $props();

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
          return [`@${block.path}`];
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
  {@const displayValue = compactToolValue(value, {
    preferPayload,
    toolName,
    tool,
  })}
  <div class="teb-row">
    <span class="teb-label">{label}</span>
    <span class:error={isError} class="teb-code">{displayValue}</span>
    {#if displayValue !== t('chat.toolNoData', '—')}
      <CopyButton
        text={displayValue}
        class="chat-copy-action tool-detail-copy"
        label={t('chat.copyToolField', 'Copy {label}', { label })}
      />
    {/if}
  </div>
{/snippet}

{#snippet reasoningSummary(isStreaming = false, isOpen = false)}
  <summary class="reasoning-header">
    <svg class="reasoning-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path
        d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
      />
      <path d="M6 13h4" />
    </svg>
    <span>{t('chat.event.thinking', 'Thinking').toUpperCase()}</span>
    {#if isStreaming}
      <span class="streaming-caret" aria-hidden="true"></span>
    {/if}
    <svg
      class="r-chevron"
      viewBox="0 0 16 16"
      width="10"
      height="10"
      style:transform={isOpen ? 'rotate(180deg)' : 'none'}
      aria-hidden="true"
    >
      <path d="M4 6l4 4 4-4" />
    </svg>
  </summary>
{/snippet}

{#snippet toolArgumentLine(summary)}
  <span class="te-arg">
    <span class="te-arg-mark">(</span>
    <span class="te-arg-value">{summary}</span>
    <span class="te-arg-mark">)</span>
  </span>
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
        <a
          class="inline-attachment"
          href={mediaUrl}
          target="_blank"
          rel="noopener noreferrer"
          use:tooltip={attachmentFilename(block)}
          aria-label={attachmentPreviewLabel(block)}
        >
          <img
            class="attachment-thumb"
            src={mediaUrl}
            alt={attachmentPreviewLabel(block)}
            loading="lazy"
          />
        </a>
        <div
          class="attachment-hover-preview"
          aria-hidden="true"
          use:floatingHoverCard={{ accessible: false }}
        >
          <img class="attachment-hover-image" src={mediaUrl} alt="" />
        </div>
        <div class="attachment-meta">
          <span class="attachment-name" use:tooltip={attachmentFilename(block)}
            >{attachmentFilename(block)}</span
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
    </div>
    <div class="msg-content">
      {#if hasReadableReasoning(item.message) && hasAssistantContent(item.message)}
        <details
          class="reasoning-block"
          open={isReasoningOpen(item.id)}
          ontoggle={(event) =>
            onReasoningOpenChange(item.id, event.currentTarget.open)}
        >
          {@render reasoningSummary(false, isReasoningOpen(item.id))}
          <div class="reasoning-body">
            <div class="reasoning-body__actions">
              <CopyButton
                text={reasoningMarkdownSource(item.message.reasoning ?? '')}
                class="chat-copy-action reasoning-copy"
                label={t('chat.copyReasoning', 'Copy thinking')}
                copiedLabel={t('chat.reasoningCopied', 'Thinking copied')}
              />
            </div>
            <MarkdownContent
              source={item.message.reasoning ?? ''}
              reasoning
              class="reasoning-markdown"
            />
          </div>
        </details>
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
            <p class="msg-body-text">{textFromMessage(item.message)}</p>
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
              <pre class="error-details-body">{errorPresentation.details}</pre>
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
              class:error={isFailedToolEvent(item.event)}
              class:running={isRunningToolEvent(item.event)}
              class:done={!isRunningToolEvent(item.event) &&
                !isFailedToolEvent(item.event)}
              class="te-dot">●</span
            >
            <span class="te-fn">{toolNameForEvent(item.event)}</span>
            {#if toolArgumentForEvent(item.event)}
              {@render toolArgumentLine(toolArgumentForEvent(item.event))}
            {/if}
          </summary>
          <div class="tool-event-body">
            {@render toolDetailSection(
              t('chat.toolArgs', 'Args'),
              toolCallFromEvent(item.event)?.arguments,
              false,
              false,
              toolNameForEvent(item.event),
              toolRowFromEvent(item.event),
            )}
            {#if toolResultValueForEvent(item.event)}
              {@render toolDetailSection(
                t('chat.toolResultLabel', 'Result'),
                toolResultValueForEvent(item.event),
                isFailedToolEvent(item.event),
                true,
                toolNameForEvent(item.event),
                toolRowFromEvent(item.event),
              )}
            {/if}
          </div>
        </details>
        {#if isTextToSpeechResult(item.event)}
          {@const speechArtifact = speechArtifactFromResult(item.event)}
          {#if speechArtifact}
            <audio
              class="speech-audio-player"
              src={speechArtifact.url}
              controls
              oncanplay={(event) => event.currentTarget.play().catch(() => {})}
            ></audio>
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
          <details
            class="reasoning-block"
            open={isReasoningOpen(item.id)}
            ontoggle={(event) =>
              onReasoningOpenChange(item.id, event.currentTarget.open)}
          >
            {@render reasoningSummary(false, isReasoningOpen(item.id))}
            <div class="reasoning-body">
              <div class="reasoning-body__actions">
                <CopyButton
                  text={reasoningMarkdownSource(
                    textFromEvent(item.event) ?? '',
                  )}
                  class="chat-copy-action reasoning-copy"
                  label={t('chat.copyReasoning', 'Copy thinking')}
                  copiedLabel={t('chat.reasoningCopied', 'Thinking copied')}
                />
              </div>
              <MarkdownContent
                source={textFromEvent(item.event) ?? ''}
                reasoning
                class="reasoning-markdown"
              />
            </div>
          </details>
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
