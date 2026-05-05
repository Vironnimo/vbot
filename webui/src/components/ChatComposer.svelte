<script>
  import { t } from '$lib/i18n.js';

  let { disabled = false, isRunning = false, onSendMessage } = $props();
  let content = $state('');
  let inputElement = $state(null);

  const submit = () => {
    const trimmedContent = content.trim();
    if (!trimmedContent || disabled) {
      return;
    }
    onSendMessage?.(trimmedContent);
    content = '';
    resizeInput();
  };

  const resizeInput = () => {
    if (!inputElement) {
      return;
    }
    inputElement.style.height = 'auto';
    inputElement.style.height = `${inputElement.scrollHeight}px`;
  };

  const handleKeydown = (event) => {
    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }
    event.preventDefault();
    submit();
  };
</script>

<form
  class="input-area"
  aria-label={t('chat.composerLabel', 'Message')}
  onsubmit={(event) => {
    event.preventDefault();
    submit();
  }}
>
  <div class="input-wrap">
    <textarea
      id="chat-composer-input"
      bind:this={inputElement}
      bind:value={content}
      class="msg-input"
      {disabled}
      aria-label={t('chat.composerLabel', 'Message')}
      oninput={resizeInput}
      onkeydown={handleKeydown}
      placeholder={t(
        'chat.composerPlaceholder',
        'Ask this agent to do something…',
      )}
      rows="1"
    ></textarea>
    <div class="input-btns">
      <button
        type="button"
        class="icon-btn"
        disabled
        aria-label={t(
          'chat.attachPlaceholder',
          'Attachments are not available yet',
        )}
        title={t('chat.attachPlaceholder', 'Attachments are not available yet')}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path
            d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
          />
        </svg>
      </button>
      <button
        type="submit"
        class="send-btn"
        disabled={disabled || !content.trim()}
        aria-label={isRunning
          ? t('chat.queueMessage', 'Queue message')
          : t('chat.sendMessage', 'Send message')}
        title={isRunning
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
  .input-area {
    width: 100%;
    min-width: 0;
  }

  .msg-input {
    height: 22px;
  }

  @media (max-width: 760px) {
    .input-area {
      padding: 12px 14px;
    }
  }

  .icon-btn svg {
    width: 14px;
    height: 14px;
  }

  .send-btn svg {
    width: 13px;
    height: 13px;
  }
</style>
