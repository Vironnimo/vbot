<script>
  import { t } from '$lib/i18n.js';

  let { disabled = false, isRunning = false, onSendMessage } = $props();
  let content = $state('');

  const submit = () => {
    const trimmedContent = content.trim();
    if (!trimmedContent || disabled) {
      return;
    }
    onSendMessage?.(trimmedContent);
    content = '';
  };

  const handleKeydown = (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      submit();
    }
  };
</script>

<form
  class="chat-composer"
  onsubmit={(event) => {
    event.preventDefault();
    submit();
  }}
>
  <label class="chat-composer__label" for="chat-composer-input">
    {t('chat.composerLabel', 'Message')}
  </label>
  <div class="chat-composer__wrap">
    <textarea
      id="chat-composer-input"
      bind:value={content}
      {disabled}
      onkeydown={handleKeydown}
      placeholder={t(
        'chat.composerPlaceholder',
        'Ask this agent to do something…',
      )}
      rows="1"
    ></textarea>
    <div class="chat-composer__actions">
      {#if isRunning}
        <span class="chat-composer__mode"
          >{t('chat.queueMessage', 'Queue message')}</span
        >
      {/if}
      <button
        class="chat-composer__send"
        type="submit"
        disabled={disabled || !content.trim()}
        aria-label={isRunning
          ? t('chat.queueMessage', 'Queue message')
          : t('chat.sendMessage', 'Send message')}
      >
        <svg viewBox="0 0 14 14" aria-hidden="true">
          <path d="M12 7L2 2l2 5-2 5 10-5z" fill="currentColor" stroke="none" />
        </svg>
      </button>
    </div>
  </div>
</form>

<style>
  .chat-composer {
    display: grid;
    flex-shrink: 0;
    gap: var(--space-xs);
    padding: 14px var(--space-lg);
    border-top: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-composer__label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .chat-composer__wrap {
    display: flex;
    align-items: flex-end;
    gap: var(--space-sm);
    padding: 11px 14px;
    border: 1px solid var(--border-2);
    border-radius: var(--r-lg);
    background: var(--bg);
    transition:
      border-color 150ms ease,
      box-shadow 150ms ease;
  }

  .chat-composer__wrap:focus-within {
    border-color: rgba(232, 135, 10, 0.4);
    box-shadow: 0 0 0 3px rgba(232, 135, 10, 0.06);
  }

  .chat-composer textarea {
    min-height: 22px;
    max-height: 182px;
    flex: 1;
    resize: vertical;
    overflow-y: auto;
    border: 0;
    outline: none;
    background: transparent;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 14px;
    line-height: 1.5;
    scrollbar-width: none;
  }

  .chat-composer textarea::-webkit-scrollbar {
    display: none;
  }

  .chat-composer__actions {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: var(--space-sm);
  }

  .chat-composer__mode {
    color: var(--amber);
    font-family: var(--font-mono);
    font-size: 10.5px;
  }

  .chat-composer__send {
    display: flex;
    width: 32px;
    height: 32px;
    align-items: center;
    justify-content: center;
    border: 1px solid rgba(232, 135, 10, 0.22);
    border-radius: var(--r-md);
    background: rgba(232, 135, 10, 0.1);
    color: var(--accent);
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }

  .chat-composer__send:hover {
    border-color: rgba(232, 135, 10, 0.4);
    background: rgba(232, 135, 10, 0.2);
  }

  .chat-composer__send svg {
    width: 13px;
    height: 13px;
  }

  .chat-composer__send:disabled,
  .chat-composer textarea:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  @media (max-width: 760px) {
    .chat-composer__mode {
      display: none;
    }
  }
</style>
