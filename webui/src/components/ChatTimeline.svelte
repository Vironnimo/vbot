<script>
  import { t } from '$lib/i18n.js';

  import { visibleTimelineItems } from '../lib/chatState.js';

  let { sessionState, agentName = '' } = $props();

  let timelineItems = $derived(visibleTimelineItems(sessionState));

  const isUserItem = (item) =>
    item.type === 'message'
      ? item.message.role === 'user'
      : item.event.type === 'user_message_persisted';

  const isAssistantItem = (item) =>
    item.type === 'message'
      ? item.message.role === 'assistant'
      : [
          'assistant_output',
          'reasoning',
          'tool_call_started',
          'tool_call_result',
        ].includes(item.event.type);

  const shouldRenderMessage = (message) =>
    Boolean(textFromMessage(message)) || hasReadableReasoning(message);

  const labelForMessage = (message) => {
    if (message.role === 'user') {
      return t('chat.role.user', 'You').toUpperCase();
    }
    if (message.role === 'assistant') {
      return t('chat.role.assistant', 'Assistant').toUpperCase();
    }
    if (message.role === 'system') {
      return t('chat.role.system', 'System').toUpperCase();
    }
    if (message.role === 'tool') {
      return t('chat.event.toolResult', 'Tool result').toUpperCase();
    }
    return t('common.unknown', 'Unknown').toUpperCase();
  };

  const labelForEvent = (event) => {
    if (event.type === 'reasoning') {
      return t('chat.event.thinking', 'Thinking').toUpperCase();
    }
    if (event.type === 'tool_call_started') {
      return t('chat.event.toolStarted', 'Tool started').toUpperCase();
    }
    if (event.type === 'tool_call_result') {
      return t('chat.event.toolResult', 'Tool result').toUpperCase();
    }
    if (event.type === 'assistant_output') {
      return t('chat.role.assistant', 'Assistant').toUpperCase();
    }
    if (event.type === 'run_completed') {
      return t('chat.event.completed', 'Run completed');
    }
    if (event.type === 'run_failed') {
      return t('chat.event.failed', 'Run failed');
    }
    if (event.type === 'run_cancelled') {
      return t('chat.event.cancelled', 'Run cancelled');
    }
    if (event.type === 'user_message_persisted') {
      return t('chat.role.user', 'You').toUpperCase();
    }
    return t('common.unknown', 'Unknown').toUpperCase();
  };

  const textFromMessage = (message) => {
    if (message.reasoning && !message.content) {
      return message.reasoning;
    }
    return message.content ?? '';
  };

  const hasReadableReasoning = (message) =>
    message.role === 'assistant' && Boolean(message.reasoning);

  const hasAssistantContent = (message) =>
    message.role === 'assistant' && Boolean(message.content);

  const messageFromEvent = (event) => event.payload?.message ?? null;

  const toolCallFromEvent = (event) => event.payload?.tool_call ?? null;

  const textFromEvent = (event) => {
    const message = messageFromEvent(event);
    if (message) {
      return textFromMessage(message);
    }
    if (event.payload?.error) {
      return event.payload.error;
    }
    return event.payload?.status ?? '';
  };

  const toolNameForEvent = (event) => {
    const toolCall = toolCallFromEvent(event);
    const message = messageFromEvent(event);
    return toolCall?.name ?? message?.name ?? t('common.unknown', 'Unknown');
  };

  const toolArgumentForEvent = (event) => {
    const toolCall = toolCallFromEvent(event);
    if (!toolCall) {
      return '';
    }
    return `(${formatJson(toolCall.arguments ?? {})})`;
  };

  const toolResultForEvent = (event) => {
    const message = messageFromEvent(event);
    return message?.content ?? '';
  };

  const formatJson = (value) => {
    if (typeof value === 'string') {
      return value;
    }
    try {
      return JSON.stringify(value ?? {});
    } catch {
      return String(value);
    }
  };

  const formatTime = (timestamp) => {
    if (!timestamp) {
      return '';
    }
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return '';
    }
    return new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    }).format(date);
  };

  const formatDate = (timestamp) => {
    if (!timestamp) {
      return t('chat.today', 'Today');
    }
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return t('chat.today', 'Today');
    }
    return new Intl.DateTimeFormat(undefined, {
      day: 'numeric',
      month: 'long',
      year: 'numeric',
    }).format(date);
  };

  const timestampForItem = (item) =>
    item.type === 'message' ? item.message.timestamp : item.event.timestamp;

  const avatarForItem = (item) => {
    if (isUserItem(item)) {
      return t('chat.role.userAvatar', 'Y');
    }
    if (isAssistantItem(item)) {
      return t('chat.role.assistantAvatar', 'A');
    }
    return t('chat.role.systemAvatar', 'S');
  };

  const metaForEvent = (event) => {
    if (event.type === 'run_failed') {
      return t('chat.runStatus.failed', 'Failed');
    }
    if (event.type === 'run_cancelled') {
      return t('chat.runStatus.cancelled', 'Cancelled');
    }
    if (event.type === 'run_completed') {
      return t('chat.runStatus.completed', 'Completed');
    }
    return '';
  };

  const isToolEvent = (event) =>
    event.type === 'tool_call_started' || event.type === 'tool_call_result';

  const isRunningToolEvent = (event) => event.type === 'tool_call_started';

  const hasErrorResult = (result) => {
    if (!result || typeof result !== 'object') {
      return false;
    }

    return Boolean(
      result.error ||
      result.ok === false ||
      result.success === false ||
      ['error', 'failed'].includes(result.status),
    );
  };

  const hasToolResultError = (event) => {
    if (event.payload?.error) {
      return true;
    }

    const content = messageFromEvent(event)?.content;
    if (!content) {
      return false;
    }

    try {
      return hasErrorResult(JSON.parse(content));
    } catch {
      return false;
    }
  };

  const isFailedToolEvent = (event) =>
    event.type === 'tool_call_result' && hasToolResultError(event);

  const isTerminalEvent = (event) => event.type.startsWith('run_');
</script>

<section class="messages" aria-live="polite">
  {#if timelineItems.length === 0}
    <div class="empty-state chat-empty-state">
      <svg class="empty-state-icon" viewBox="0 0 32 32" aria-hidden="true">
        <path d="M5 7h22v14H16l-6 5v-5H5z" />
      </svg>
      <p class="empty-state-title">
        {t('chat.historyEmptyTitle', 'No messages yet')}
      </p>
      <p class="empty-state-sub">
        {t(
          'chat.historyEmpty',
          'No messages yet. Send the first message to this agent.',
        )}
      </p>
    </div>
  {:else}
    <div class="date-sep">{formatDate(timestampForItem(timelineItems[0]))}</div>
    {#each timelineItems as item (item.id)}
      {#if item.type === 'message' && shouldRenderMessage(item.message)}
        <article
          class:assistant={item.message.role === 'assistant'}
          class:user={item.message.role === 'user'}
          class="msg"
        >
          <div class="msg-header">
            <div class="msg-avatar">{avatarForItem(item)}</div>
            <span class="msg-author">{labelForMessage(item.message)}</span>
            {#if formatTime(item.message.timestamp)}
              <span class="msg-timestamp"
                >{formatTime(item.message.timestamp)}</span
              >
            {/if}
            {#if item.message.role === 'assistant' && agentName}
              <span class="msg-meta-extra">· {agentName}</span>
            {/if}
          </div>
          <div class="msg-content">
            {#if hasReadableReasoning(item.message) && hasAssistantContent(item.message)}
              <details class="reasoning-block">
                <summary class="reasoning-header">
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path
                      d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
                    />
                    <path d="M6 13h4" />
                  </svg>
                  {t('chat.event.thinking', 'Thinking').toUpperCase()}
                  <svg class="r-chevron" viewBox="0 0 16 16" aria-hidden="true">
                    <path d="M4 6l4 4 4-4" />
                  </svg>
                </summary>
                <div class="reasoning-body">{item.message.reasoning}</div>
              </details>
            {/if}
            {#if textFromMessage(item.message)}
              <p class="msg-body-text">{textFromMessage(item.message)}</p>
            {/if}
          </div>
        </article>
      {:else if item.type === 'event'}
        {#if isToolEvent(item.event)}
          <article class="msg assistant">
            <div class="msg-header">
              <div class="msg-avatar">{avatarForItem(item)}</div>
              <span class="msg-author">{labelForEvent(item.event)}</span>
              {#if formatTime(item.event.timestamp)}
                <span class="msg-timestamp"
                  >{formatTime(item.event.timestamp)}</span
                >
              {/if}
            </div>
            <div class="msg-content">
              <details
                class="tool-event"
                open={!isRunningToolEvent(item.event)}
              >
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
                    <span class="te-arg"
                      >{toolArgumentForEvent(item.event)}</span
                    >
                  {/if}
                  <span
                    class:error={isFailedToolEvent(item.event)}
                    class="te-time"
                  >
                    {isFailedToolEvent(item.event)
                      ? t('chat.runStatus.failed', 'Failed')
                      : isRunningToolEvent(item.event)
                        ? t('chat.runStatus.running', 'Running')
                        : t('chat.toolDone', 'done')}
                  </span>
                </summary>
                <div class="tool-event-body">
                  {#if toolArgumentForEvent(item.event)}
                    <div class="teb-row">
                      <span class="teb-label">{t('chat.toolArgs', 'Args')}</span
                      >
                      <span class="teb-code"
                        >{toolArgumentForEvent(item.event)}</span
                      >
                    </div>
                  {/if}
                  {#if toolResultForEvent(item.event)}
                    <div class="teb-row">
                      <span class="teb-label"
                        >{t('chat.toolResultLabel', 'Result')}</span
                      >
                      <span
                        class:error={isFailedToolEvent(item.event)}
                        class="teb-code result"
                        >{toolResultForEvent(item.event)}</span
                      >
                    </div>
                  {/if}
                </div>
              </details>
            </div>
          </article>
        {:else if isTerminalEvent(item.event)}
          <p class="chat-terminal-event">
            <span>{labelForEvent(item.event)}</span>
            {#if metaForEvent(item.event)}
              <span>· {metaForEvent(item.event)}</span>
            {/if}
          </p>
        {:else if textFromEvent(item.event)}
          <article
            class:assistant={isAssistantItem(item)}
            class:user={isUserItem(item)}
            class="msg"
          >
            <div class="msg-header">
              <div class="msg-avatar">{avatarForItem(item)}</div>
              <span class="msg-author">{labelForEvent(item.event)}</span>
              {#if formatTime(item.event.timestamp)}
                <span class="msg-timestamp"
                  >{formatTime(item.event.timestamp)}</span
                >
              {/if}
            </div>
            <div class="msg-content">
              {#if item.event.type === 'reasoning'}
                <details class="reasoning-block">
                  <summary class="reasoning-header">
                    <svg viewBox="0 0 16 16" aria-hidden="true">
                      <path
                        d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
                      />
                      <path d="M6 13h4" />
                    </svg>
                    {t('chat.event.thinking', 'Thinking').toUpperCase()}
                    <svg
                      class="r-chevron"
                      viewBox="0 0 16 16"
                      aria-hidden="true"
                    >
                      <path d="M4 6l4 4 4-4" />
                    </svg>
                  </summary>
                  <div class="reasoning-body">{textFromEvent(item.event)}</div>
                </details>
              {:else}
                <p class="msg-body-text">{textFromEvent(item.event)}</p>
              {/if}
            </div>
          </article>
        {/if}
      {/if}
    {/each}
  {/if}
</section>

<style>
  .messages {
    min-height: 0;
    background: var(--bg);
  }

  .chat-empty-state {
    min-height: 100%;
  }

  .empty-state-icon {
    width: 38px;
    height: 38px;
  }

  .msg-body-text,
  .reasoning-body,
  .teb-code,
  .chat-terminal-event {
    white-space: pre-wrap;
  }

  .reasoning-block,
  .tool-event {
    max-width: 100%;
  }

  .reasoning-block summary,
  .tool-event summary {
    cursor: pointer;
    list-style: none;
  }

  .reasoning-block summary::-webkit-details-marker,
  .tool-event summary::-webkit-details-marker {
    display: none;
  }

  .reasoning-header svg:first-child {
    width: 10px;
    height: 10px;
    opacity: 0.4;
  }

  .reasoning-block[open] .reasoning-body,
  .tool-event[open] .tool-event-body {
    display: flex;
  }

  .reasoning-block[open] .reasoning-body {
    display: block;
  }

  .reasoning-block[open] .r-chevron {
    transform: rotate(180deg);
  }

  .tool-event-body {
    max-width: min(64rem, calc(100vw - 340px));
  }

  .teb-code {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .chat-terminal-event {
    align-self: center;
    margin: 8px 28px;
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
    text-align: center;
  }
</style>
