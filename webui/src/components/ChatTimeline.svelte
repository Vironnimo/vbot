<script>
  import { t } from '$lib/i18n.js';

  import { visibleTimelineItems } from '../lib/chatState.js';

  let { sessionState } = $props();

  let timelineItems = $derived(visibleTimelineItems(sessionState));

  const labelForMessage = (message) => {
    if (message.role === 'user') {
      return t('chat.role.user', 'You');
    }
    if (message.role === 'assistant') {
      return t('chat.role.assistant', 'Assistant');
    }
    if (message.role === 'system') {
      return t('chat.role.system', 'System');
    }
    if (message.role === 'tool') {
      return t('chat.event.toolResult', 'Tool result');
    }
    return t('common.unknown', 'Unknown');
  };

  const labelForEvent = (event) => {
    if (event.type === 'reasoning') {
      return t('chat.event.thinking', 'Thinking');
    }
    if (event.type === 'tool_call_started') {
      return t('chat.event.toolStarted', 'Tool started');
    }
    if (event.type === 'tool_call_result') {
      return t('chat.event.toolResult', 'Tool result');
    }
    if (event.type === 'assistant_output') {
      return t('chat.event.assistantOutput', 'Assistant output');
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
      return t('chat.role.user', 'You');
    }
    return t('common.unknown', 'Unknown');
  };

  const textFromMessage = (message) => {
    if (message.reasoning && !message.content) {
      return message.reasoning;
    }
    return message.content ?? '';
  };

  const timeLabel = (timestamp) => {
    if (!timestamp) {
      return '';
    }
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) {
      return timestamp;
    }
    return date.toLocaleTimeString([], {
      hour: 'numeric',
      minute: '2-digit',
    });
  };

  const hasReadableReasoning = (message) =>
    message.role === 'assistant' && Boolean(message.reasoning);

  const hasAssistantContent = (message) =>
    message.role === 'assistant' && Boolean(message.content);

  const textFromEvent = (event) => {
    const message = event.payload?.message;
    if (message) {
      return textFromMessage(message);
    }
    if (event.payload?.tool_call) {
      const toolCall = event.payload.tool_call;
      return `${toolCall.name ?? t('common.unknown', 'Unknown')} ${JSON.stringify(toolCall.arguments ?? {})}`;
    }
    if (event.payload?.error) {
      return event.payload.error;
    }
    return event.payload?.status ?? '';
  };

  const formatJson = (value) => {
    if (value === undefined || value === null || value === '') {
      return '';
    }
    if (typeof value === 'string') {
      return value;
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  };

  const toolCallFromEvent = (event) => event.payload?.tool_call ?? null;

  const toolResultMessageFromEvent = (event) => event.payload?.message ?? null;

  const toolNameFromEvent = (event) => {
    const toolCall = toolCallFromEvent(event);
    const toolMessage = toolResultMessageFromEvent(event);
    return (
      toolCall?.name ?? toolMessage?.name ?? t('common.unknown', 'Unknown')
    );
  };

  const toolArgumentsFromEvent = (event) =>
    formatJson(toolCallFromEvent(event)?.arguments);

  const toolResultFromEvent = (event) =>
    formatJson(toolResultMessageFromEvent(event)?.content);

  const hasToolDetails = (event) =>
    Boolean(toolArgumentsFromEvent(event) || toolResultFromEvent(event));

  const toolDotClass = (event) => {
    if (event.type === 'tool_call_started') {
      return 'running';
    }
    if (event.payload?.error) {
      return 'error';
    }
    return 'done';
  };

  const eventStatusText = (event) => {
    if (event.type === 'tool_call_started') {
      return t('chat.event.running', 'running…');
    }
    if (event.type === 'tool_call_result') {
      return event.payload?.error
        ? t('chat.event.failed', 'Run failed')
        : t('chat.event.done', 'done');
    }
    return textFromEvent(event);
  };
</script>

<section class="chat-timeline" aria-live="polite">
  {#if timelineItems.length === 0}
    <p class="chat-timeline__empty">
      {t(
        'chat.historyEmpty',
        'No messages yet. Send the first message to this agent.',
      )}
    </p>
  {:else}
    {#each timelineItems as item (item.id)}
      {#if item.type === 'message'}
        {#if hasReadableReasoning(item.message) && hasAssistantContent(item.message)}
          <details class="reasoning-block">
            <summary class="reasoning-header">
              {t('chat.event.thinking', 'Thinking')}
            </summary>
            <p class="reasoning-body">{item.message.reasoning}</p>
          </details>
        {/if}
        <article
          class:msg--user={item.message.role === 'user'}
          class:msg--assistant={item.message.role === 'assistant'}
          class="msg"
        >
          <div class="msg-header">
            <div class="msg-avatar">
              {labelForMessage(item.message).slice(0, 1)}
            </div>
            <span class="msg-author">{labelForMessage(item.message)}</span>
            {#if item.message.timestamp}
              <span class="msg-timestamp"
                >{timeLabel(item.message.timestamp)}</span
              >
            {/if}
          </div>
          <div class="msg-content">
            <p class="msg-body-text">{textFromMessage(item.message)}</p>
          </div>
        </article>
      {:else if item.event.type === 'reasoning'}
        <details class="reasoning-block">
          <summary class="reasoning-header">
            {labelForEvent(item.event)}
          </summary>
          <p class="reasoning-body">{textFromEvent(item.event)}</p>
        </details>
      {:else if item.event.type === 'tool_call_started' || item.event.type === 'tool_call_result'}
        <details class="tool-event" open={hasToolDetails(item.event)}>
          <summary class="tool-event-line">
            <span class={`te-dot ${toolDotClass(item.event)}`}>●</span>
            <span class="te-fn">{toolNameFromEvent(item.event)}</span>
            <span class="te-time">{eventStatusText(item.event)}</span>
          </summary>
          {#if hasToolDetails(item.event)}
            <div class="tool-event-body">
              {#if toolArgumentsFromEvent(item.event)}
                <div class="teb-row">
                  <span class="teb-label">{t('chat.event.args', 'Args')}</span>
                  <pre class="teb-code">{toolArgumentsFromEvent(
                      item.event,
                    )}</pre>
                </div>
              {/if}
              {#if toolResultFromEvent(item.event)}
                <div class="teb-row">
                  <span class="teb-label"
                    >{t('chat.event.result', 'Result')}</span
                  >
                  <pre class="teb-code">{toolResultFromEvent(item.event)}</pre>
                </div>
              {/if}
            </div>
          {/if}
        </details>
      {:else if item.event.type.startsWith('run_')}
        <p class="run-status-line">
          <span>{labelForEvent(item.event)}</span>
          {#if textFromEvent(item.event)}
            <span>{textFromEvent(item.event)}</span>
          {/if}
        </p>
      {:else}
        <article
          class:msg--user={item.event.type === 'user_message_persisted'}
          class:msg--assistant={item.event.type === 'assistant_output'}
          class="msg"
        >
          <div class="msg-header">
            <div class="msg-avatar">
              {labelForEvent(item.event).slice(0, 1)}
            </div>
            <span class="msg-author">{labelForEvent(item.event)}</span>
            {#if item.event.timestamp}
              <span class="msg-timestamp"
                >{timeLabel(item.event.timestamp)}</span
              >
            {/if}
          </div>
          <div class="msg-content">
            <p class="msg-body-text">{textFromEvent(item.event)}</p>
          </div>
        </article>
      {/if}
    {/each}
  {/if}
</section>

<style>
  .chat-timeline {
    display: flex;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    gap: 10px;
    overflow-y: auto;
    padding: var(--space-xl) 0;
    scroll-behavior: smooth;
  }

  .chat-timeline__empty {
    margin: auto;
    max-width: 24rem;
    color: var(--text-lo);
    font-family: var(--font-ui);
    line-height: 1.6;
    text-align: center;
  }

  .msg {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 6px var(--space-xl);
  }

  .msg-header {
    display: flex;
    align-items: center;
    gap: var(--space-sm);
  }

  .msg-avatar {
    display: flex;
    width: 26px;
    height: 26px;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    border-radius: var(--r-sm);
    background: var(--surface-3);
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
  }

  .msg-author {
    color: var(--text-med);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }

  .msg-timestamp,
  .run-status-line {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10.5px;
  }

  .msg-content {
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding-left: 34px;
  }

  .msg-body-text {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 14px;
    line-height: 1.65;
    white-space: pre-wrap;
  }

  .msg--user {
    align-items: flex-end;
  }

  .msg--user .msg-header {
    flex-direction: row-reverse;
  }

  .msg--user .msg-avatar {
    background: var(--accent-pale);
    color: var(--accent);
  }

  .msg--user .msg-author {
    color: var(--accent);
  }

  .msg--user .msg-content {
    width: 100%;
    align-items: flex-end;
    padding-right: 34px;
    padding-left: 0;
  }

  .msg--user .msg-body-text {
    max-width: 75%;
    padding: 10px 16px;
    border: 1px solid var(--border-2);
    border-left: 3px solid var(--accent);
    border-radius: 0 var(--r-md) var(--r-md) 0;
    background: var(--surface-2);
    line-height: 1.55;
  }

  .reasoning-block,
  .tool-event {
    align-self: flex-start;
    margin: 0 var(--space-xl) 0 calc(var(--space-xl) + 34px);
  }

  .reasoning-header,
  .tool-event-line {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--text-med);
    cursor: pointer;
    font-family: var(--font-ui);
    font-size: 12px;
    user-select: none;
  }

  .reasoning-header:hover,
  .tool-event-line:hover {
    color: var(--text-hi);
  }

  .reasoning-body {
    margin: 4px 0 0;
    padding: 6px 0 2px 16px;
    border-left: 2px solid var(--border-2);
    color: var(--text-med);
    font-size: 13px;
    font-style: italic;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .te-dot {
    flex-shrink: 0;
    font-size: 10px;
  }

  .te-dot.done {
    color: var(--green);
  }

  .te-dot.running {
    animation: blink 1.2s ease-in-out infinite;
    color: var(--amber);
  }

  .te-dot.error {
    color: var(--red);
  }

  .te-fn,
  .te-time,
  .teb-code,
  .teb-label {
    font-family: var(--font-mono);
  }

  .te-fn {
    color: var(--text-med);
    font-size: 12px;
    font-weight: 500;
  }

  .te-time {
    color: var(--text-lo);
    font-size: 10.5px;
  }

  .tool-event-body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 3px;
    padding: 5px 0 3px 18px;
    border-left: 1px solid var(--border-2);
  }

  .teb-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-sm);
  }

  .teb-label {
    min-width: 46px;
    flex-shrink: 0;
    padding-top: 1px;
    color: var(--text-lo);
    font-size: 10px;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .teb-code {
    max-width: min(48rem, calc(100vw - 360px));
    margin: 0;
    overflow-x: auto;
    color: var(--text-med);
    font-size: 11.5px;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .run-status-line {
    display: flex;
    align-self: center;
    gap: var(--space-sm);
    margin: 4px var(--space-xl);
  }

  @keyframes blink {
    0%,
    100% {
      opacity: 1;
    }

    50% {
      opacity: 0.3;
    }
  }
</style>
