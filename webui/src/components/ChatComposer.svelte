<script>
  import { tick } from 'svelte';

  import { t } from '$lib/i18n.js';
  import SkillAutocomplete from './SkillAutocomplete.svelte';

  const SKILL_TRIGGER_PATTERN = /[A-Za-z0-9_-]/u;

  let {
    disabled = false,
    isRunning = false,
    availableSkills = [],
    onSendMessage,
  } = $props();
  let content = $state('');
  let inputElement = $state(null);
  let autocompleteElement = $state(null);
  let triggerContext = $state(null);
  let activeSkillIndex = $state(0);

  let loadableSkills = $derived(availableSkills.filter((skill) => skill?.name));
  let autocompleteQuery = $derived.by(() => {
    if (!triggerContext) {
      return '';
    }

    return content.slice(triggerContext.start + 1, triggerContext.end);
  });
  let showSkillAutocomplete = $derived(
    Boolean(triggerContext) && matchingSkillCount() > 0,
  );

  const submit = () => {
    const trimmedContent = content.trim();
    if (!trimmedContent || disabled) {
      return;
    }
    onSendMessage?.(content);
    content = '';
    triggerContext = null;
    activeSkillIndex = 0;
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
    if (showSkillAutocomplete) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        activeSkillIndex = Math.min(
          activeSkillIndex + 1,
          matchingSkillCount() - 1,
        );
        return;
      }

      if (event.key === 'ArrowUp') {
        event.preventDefault();
        activeSkillIndex = Math.max(activeSkillIndex - 1, 0);
        return;
      }

      if (event.key === 'Tab') {
        if (autocompleteElement?.selectActive()) {
          event.preventDefault();
        }
        return;
      }

      if (event.key === 'Escape') {
        event.preventDefault();
        triggerContext = null;
        activeSkillIndex = 0;
        return;
      }
    }

    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }

    if (showSkillAutocomplete && autocompleteElement?.selectActive()) {
      event.preventDefault();
      return;
    }

    event.preventDefault();
    submit();
  };

  const handleInput = () => {
    resizeInput();
    updateTriggerContext();
  };

  const handleSelection = () => {
    updateTriggerContext();
  };

  const matchingSkillCount = () => {
    if (!triggerContext) {
      return 0;
    }

    const normalizedQuery = autocompleteQuery.trim().toLowerCase();
    const matchingSkills = normalizedQuery
      ? loadableSkills.filter((skill) =>
          `${skill.name} ${skill.description ?? ''}`
            .toLowerCase()
            .includes(normalizedQuery),
        )
      : loadableSkills;

    return Math.min(matchingSkills.length, 8);
  };

  const updateTriggerContext = () => {
    if (!inputElement) {
      triggerContext = null;
      activeSkillIndex = 0;
      return;
    }

    const cursorPosition = inputElement.selectionStart ?? content.length;
    triggerContext = detectSkillTrigger(content, cursorPosition);
    activeSkillIndex = 0;
  };

  const detectSkillTrigger = (value, cursorPosition) => {
    const boundedCursor = Math.max(0, Math.min(cursorPosition, value.length));
    let start = boundedCursor - 1;

    while (start >= 0 && SKILL_TRIGGER_PATTERN.test(value[start])) {
      start -= 1;
    }

    if (start < 0) {
      return null;
    }

    const trigger = value[start];

    if (trigger !== '/' && trigger !== '$') {
      return null;
    }

    if (trigger === '/' && start !== 0) {
      return null;
    }

    if (
      trigger === '$' &&
      start > 0 &&
      SKILL_TRIGGER_PATTERN.test(value[start - 1])
    ) {
      return null;
    }

    for (let index = start + 1; index < boundedCursor; index += 1) {
      if (!SKILL_TRIGGER_PATTERN.test(value[index])) {
        return null;
      }
    }

    return { marker: trigger, start, end: boundedCursor };
  };

  const selectSkill = async (skill) => {
    if (!triggerContext || !skill?.name) {
      return;
    }

    const prefix = content.slice(0, triggerContext.start);
    const suffix = content.slice(triggerContext.end);
    const insertedToken = `${triggerContext.marker}${skill.name}`;
    const nextCursorPosition = prefix.length + insertedToken.length;
    content = `${prefix}${insertedToken}${suffix}`;
    triggerContext = null;
    activeSkillIndex = 0;

    await tick();
    inputElement?.focus();
    inputElement?.setSelectionRange(nextCursorPosition, nextCursorPosition);
    resizeInput();
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
  {#if showSkillAutocomplete}
    <SkillAutocomplete
      bind:this={autocompleteElement}
      skills={loadableSkills}
      query={autocompleteQuery}
      activeIndex={activeSkillIndex}
      onSelect={selectSkill}
      onHover={(index) => {
        activeSkillIndex = index;
      }}
    />
  {/if}
  <div class="input-wrap">
    <textarea
      id="chat-composer-input"
      bind:this={inputElement}
      bind:value={content}
      class="msg-input"
      {disabled}
      aria-label={t('chat.composerLabel', 'Message')}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onclick={handleSelection}
      onkeyup={handleSelection}
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
    position: relative;
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
