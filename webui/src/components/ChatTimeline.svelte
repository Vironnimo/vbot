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
        <article
          class:chat-timeline__item--user={item.message.role === 'user'}
          class:chat-timeline__item--assistant={item.message.role ===
            'assistant'}
          class="chat-timeline__item"
        >
          <p class="chat-timeline__label">{labelForMessage(item.message)}</p>
          <p class="chat-timeline__content">{textFromMessage(item.message)}</p>
        </article>
      {:else}
        <article
          class:chat-timeline__item--user={item.event.type ===
            'user_message_persisted'}
          class:chat-timeline__item--assistant={item.event.type ===
            'assistant_output'}
          class:chat-timeline__item--terminal={item.event.type.startsWith(
            'run_',
          )}
          class="chat-timeline__item"
        >
          <p class="chat-timeline__label">{labelForEvent(item.event)}</p>
          <p class="chat-timeline__content">{textFromEvent(item.event)}</p>
        </article>
      {/if}
    {/each}
  {/if}
</section>

<style>
  .chat-timeline {
    display: flex;
    min-height: 24rem;
    flex-direction: column;
    gap: var(--space-md);
    overflow-y: auto;
    padding: var(--space-lg);
  }

  .chat-timeline__empty {
    margin: auto;
    max-width: 24rem;
    color: var(--color-muted);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    line-height: 1.6;
    text-align: center;
  }

  .chat-timeline__item {
    max-width: min(42rem, 86%);
    padding: var(--space-md);
    border: 1px solid rgba(240, 164, 58, 0.14);
    border-radius: var(--radius-md);
    background: rgba(21, 19, 15, 0.72);
  }

  .chat-timeline__item--user {
    align-self: flex-end;
    border-color: rgba(255, 202, 115, 0.28);
    background: rgba(240, 164, 58, 0.12);
  }

  .chat-timeline__item--assistant {
    align-self: flex-start;
    background: rgba(45, 39, 31, 0.88);
  }

  .chat-timeline__item--terminal {
    align-self: center;
    padding-block: var(--space-sm);
    color: var(--color-muted);
    text-align: center;
  }

  .chat-timeline__label {
    margin: 0 0 var(--space-xs);
    color: var(--color-accent);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .chat-timeline__content {
    margin: 0;
    color: var(--color-text);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    line-height: 1.65;
    white-space: pre-wrap;
  }
</style>
