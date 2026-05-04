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
</script>

<form
  class="chat-composer"
  onsubmit={(event) => {
    event.preventDefault();
    submit();
  }}
>
  <label for="chat-composer-input">{t('chat.composerLabel', 'Message')}</label>
  <div class="chat-composer__row">
    <textarea
      id="chat-composer-input"
      bind:value={content}
      {disabled}
      placeholder={t(
        'chat.composerPlaceholder',
        'Ask this agent to do something…',
      )}
      rows="3"
    ></textarea>
    <button type="submit" disabled={disabled || !content.trim()}>
      {isRunning
        ? t('chat.queueMessage', 'Queue message')
        : t('common.send', 'Send')}
    </button>
  </div>
</form>

<style>
  .chat-composer {
    display: grid;
    gap: var(--space-sm);
    padding: var(--space-md);
    border-top: 1px solid var(--color-border);
    background: rgba(21, 19, 15, 0.78);
  }

  .chat-composer label {
    color: var(--color-accent);
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .chat-composer__row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--space-sm);
    align-items: end;
  }

  .chat-composer textarea {
    width: 100%;
    resize: vertical;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    padding: var(--space-md);
    color: var(--color-text);
    background: rgba(10, 11, 12, 0.62);
    font: inherit;
    line-height: 1.5;
  }

  .chat-composer button {
    min-height: 3.25rem;
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    padding: 0 var(--space-lg);
    color: #1f1608;
    background: var(--color-accent-strong);
    cursor: pointer;
    font-family: 'Trebuchet MS', Verdana, sans-serif;
    font-weight: 700;
  }

  .chat-composer button:disabled,
  .chat-composer textarea:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  @media (max-width: 760px) {
    .chat-composer__row {
      grid-template-columns: 1fr;
    }
  }
</style>
