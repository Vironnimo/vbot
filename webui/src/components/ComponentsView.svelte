<script>
  import { t } from '$lib/i18n.js';

  const simpleOptions = [
    {
      id: 'option-a',
      labelKey: 'components.dropdowns.optionA',
      labelFallback: 'Option A',
    },
    {
      id: 'option-b',
      labelKey: 'components.dropdowns.optionB',
      labelFallback: 'Option B',
    },
    {
      id: 'option-c',
      labelKey: 'components.dropdowns.optionC',
      labelFallback: 'Option C',
    },
    {
      id: 'option-d',
      labelKey: 'components.dropdowns.optionD',
      labelFallback: 'Option D',
    },
  ];
  const modelOptions = [
    {
      id: 'model-anthropic-sonnet',
      labelKey: 'components.models.anthropicSonnet',
      labelFallback: 'showcase/anthropic-sonnet',
    },
    {
      id: 'model-openai-primary',
      labelKey: 'components.models.openAiPrimary',
      labelFallback: 'showcase/openai-primary',
    },
    {
      id: 'model-openai-compact',
      labelKey: 'components.models.openAiCompact',
      labelFallback: 'showcase/openai-compact',
    },
    {
      id: 'model-openrouter-gemini',
      labelKey: 'components.models.openRouterGemini',
      labelFallback: 'showcase/openrouter-gemini',
    },
    {
      id: 'model-local-llama',
      labelKey: 'components.models.localLlama',
      labelFallback: 'showcase/local-llama',
    },
  ];

  let toasts = $state([]);
  let inlineConfirmOpen = $state(false);
  let simpleDropdownOpen = $state(false);
  let searchableDropdownOpen = $state(false);
  let selectedSimpleOptionId = $state(simpleOptions[0].id);
  let selectedModelId = $state(modelOptions[1].id);
  let modelFilter = $state('');
  let largeToggleOn = $state(true);
  let largeToggleOff = $state(false);
  let smallToggleOn = $state(true);
  let smallToggleOff = $state(false);
  let thinkingOpen = $state(false);
  let toolEventOpen = $state(true);

  let filteredModelOptions = $derived.by(() => {
    const normalizedFilter = modelFilter.trim().toLowerCase();

    return modelOptions.filter((option) =>
      t(option.labelKey, option.labelFallback)
        .toLowerCase()
        .includes(normalizedFilter),
    );
  });
  let selectedSimpleOption = $derived(
    simpleOptions.find((option) => option.id === selectedSimpleOptionId) ??
      simpleOptions[0],
  );
  let selectedSimpleOptionLabel = $derived(
    t(selectedSimpleOption.labelKey, selectedSimpleOption.labelFallback),
  );
  let selectedModel = $derived(
    modelOptions.find((option) => option.id === selectedModelId) ??
      modelOptions[0],
  );
  let selectedModelLabel = $derived(
    t(selectedModel.labelKey, selectedModel.labelFallback),
  );

  function showToast(type) {
    const toastContent = {
      success: {
        title: t('components.toast.successTitle', 'Agent saved.'),
        message: t(
          'components.toast.successMessage',
          'Changes have been applied.',
        ),
      },
      error: {
        title: t('components.toast.errorTitle', 'Connection failed.'),
        message: t(
          'components.toast.errorMessage',
          'Showcase-only connection warning; no runtime health check was made.',
        ),
      },
      warn: {
        title: t('components.toast.warnTitle', 'Rate limit approaching.'),
        message: t(
          'components.toast.warnMessage',
          'Slowing requests to avoid HTTP 429.',
        ),
      },
      info: {
        title: t('components.toast.infoTitle', 'Session resumed.'),
        message: t(
          'components.toast.infoMessage',
          'Picked up from checkpoint #7.',
        ),
      },
    };

    toasts = [
      ...toasts,
      {
        id: crypto.randomUUID(),
        type,
        ...toastContent[type],
      },
    ];
  }

  function dismissToast(toastId) {
    toasts = toasts.filter((toast) => toast.id !== toastId);
  }

  function selectSimpleOption(option) {
    selectedSimpleOptionId = option.id;
    simpleDropdownOpen = false;
  }

  function selectModel(option) {
    selectedModelId = option.id;
    searchableDropdownOpen = false;
    modelFilter = '';
  }
</script>

<section class="comp-layout view active" aria-labelledby="components-title">
  <div class="comp-head">
    <h2 id="components-title" class="comp-head-title">
      {t('components.title', 'Components')}
    </h2>
    <p class="comp-head-sub">
      {t(
        'components.subtitle',
        'All defined UI primitives. Click, hover, and interact with each element.',
      )}
    </p>
  </div>

  <div class="comp-body">
    <section class="comp-section" aria-labelledby="components-buttons-title">
      <h3 id="components-buttons-title" class="comp-section-title">
        {t('components.sections.buttons', 'Buttons')}
      </h3>
      <div class="comp-row">
        <button class="btn-new" type="button">
          <svg viewBox="0 0 14 14" aria-hidden="true"
            ><path d="M7 1v12M1 7h12" /></svg
          >
          {t('chat.newSession', 'New session')}
        </button>
        <button class="btn-outline" type="button"
          >{t('common.edit', 'Edit')}</button
        >
        <button class="btn-outline btn-dang" type="button"
          >{t('common.archive', 'Archive')}</button
        >
        <button class="modal-btn-confirm" type="button"
          >{t('common.confirm', 'Confirm')}</button
        >
        <button class="modal-btn-cancel" type="button"
          >{t('common.cancel', 'Cancel')}</button
        >
        <button class="tl-btn" type="button"
          >{t('components.buttons.allOn', 'all on')}</button
        >
        <button class="tl-btn" type="button"
          >{t('components.buttons.allOff', 'all off')}</button
        >
        <button
          class="icon-btn"
          type="button"
          data-tooltip={t('components.tooltips.attachFile', 'Attach file')}
          aria-label={t('components.tooltips.attachFile', 'Attach file')}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path
              d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
            />
          </svg>
        </button>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-toasts-title">
      <h3 id="components-toasts-title" class="comp-section-title">
        {t('components.sections.toasts', 'Toasts')}
      </h3>
      <div class="comp-row">
        <button
          class="btn-outline"
          type="button"
          onclick={() => showToast('success')}
        >
          {t('components.toast.success', 'Success')}
        </button>
        <button
          class="btn-outline"
          type="button"
          onclick={() => showToast('error')}
        >
          {t('components.toast.error', 'Error')}
        </button>
        <button
          class="btn-outline"
          type="button"
          onclick={() => showToast('warn')}
        >
          {t('components.toast.warning', 'Warning')}
        </button>
        <button
          class="btn-outline"
          type="button"
          onclick={() => showToast('info')}
        >
          {t('components.toast.info', 'Info')}
        </button>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-code-title">
      <h3 id="components-code-title" class="comp-section-title">
        {t('components.sections.codeBlock', 'Code block')}
      </h3>
      <div class="msg-code showcase-code">
        <div class="msg-code-header">
          <span class="msg-code-lang">
            {t('components.code.languagePython', 'python')}
          </span>
          <button
            class="icon-btn icon-btn--small"
            type="button"
            data-tooltip={t('common.copy', 'Copy')}
            aria-label={t('common.copy', 'Copy')}
          >
            <svg viewBox="0 0 14 14" aria-hidden="true">
              <rect x="1" y="4" width="9" height="9" rx="1.5" />
              <path
                d="M4 4V2.5A1.5 1.5 0 0 1 5.5 1H11.5A1.5 1.5 0 0 1 13 2.5v6A1.5 1.5 0 0 1 11.5 10H10"
              />
            </svg>
          </button>
        </div>
        <pre><code
            >{t(
              'components.code.pythonSample',
              `def toasted_sample(count: int) -> int:
    if count <= 1:
        return count
    return toasted_sample(count - 1) + toasted_sample(count - 2)

print(toasted_sample(10))  # 55`,
            )}</code
          ></pre>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-empty-title">
      <h3 id="components-empty-title" class="comp-section-title">
        {t('components.sections.emptyState', 'Empty state')}
      </h3>
      <div class="empty-state showcase-empty">
        <svg class="empty-state-icon" viewBox="0 0 32 32" aria-hidden="true">
          <path d="M4 6h24v18a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6z" />
          <path d="M4 6h24M10 6V4h12v2" />
        </svg>
        <div class="empty-state-title">
          {t('chat.empty.title', 'No messages yet')}
        </div>
        <div class="empty-state-sub">
          {t(
            'chat.empty.subtitle',
            'Send a message to start the conversation.',
          )}
        </div>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-confirm-title">
      <h3 id="components-confirm-title" class="comp-section-title">
        {t('components.sections.inlineConfirm', 'Inline confirm')}
      </h3>
      {#if inlineConfirmOpen}
        <div class="inline-confirm">
          <span class="inline-confirm-label">
            {t('components.inlineConfirm.question', 'Archive this agent?')}
          </span>
          <button
            class="modal-btn-cancel"
            type="button"
            onclick={() => (inlineConfirmOpen = false)}
          >
            {t('common.cancel', 'Cancel')}
          </button>
          <button
            class="modal-btn-confirm"
            type="button"
            onclick={() => (inlineConfirmOpen = false)}
          >
            {t('common.confirm', 'Confirm')}
          </button>
        </div>
      {:else}
        <button
          class="btn-outline btn-dang"
          type="button"
          onclick={() => (inlineConfirmOpen = true)}
        >
          {t('components.inlineConfirm.archiveAgent', 'Archive agent')}
        </button>
      {/if}
    </section>

    <section class="comp-section" aria-labelledby="components-inputs-title">
      <h3 id="components-inputs-title" class="comp-section-title">
        {t('components.sections.inputs', 'Inputs')}
      </h3>
      <div class="input-showcase">
        <input
          class="s-input"
          type="text"
          placeholder={t('components.inputs.settings', 'Settings input…')}
        />
        <input
          class="s-input"
          type="password"
          placeholder={t('components.inputs.apiKey', 'Password / API key…')}
        />
        <input
          class="modal-input"
          type="text"
          placeholder={t('components.inputs.modal', 'Modal input (larger)…')}
        />
        <div class="input-wrap">
          <textarea
            class="msg-input"
            placeholder={t('chat.composer.placeholder', 'Enter message…')}
            rows="1"
          ></textarea>
          <div class="input-btns">
            <button
              class="icon-btn"
              type="button"
              aria-label={t('components.tooltips.attachFile', 'Attach file')}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                <path
                  d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
                />
              </svg>
            </button>
            <button
              class="send-btn"
              type="button"
              aria-label={t('chat.send', 'Send')}
            >
              <svg viewBox="0 0 14 14" aria-hidden="true">
                <path
                  d="M12 7L2 2l2 5-2 5 10-5z"
                  fill="currentColor"
                  stroke="none"
                />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-dropdown-title">
      <h3 id="components-dropdown-title" class="comp-section-title">
        {t('components.sections.dropdowns', 'Dropdowns')}
      </h3>
      <div class="comp-row comp-row--top">
        <div class="field-stack">
          <span class="micro-label"
            >{t('components.dropdowns.simple', 'Simple')}</span
          >
          <div
            class:open={simpleDropdownOpen}
            class="dropdown showcase-dropdown"
          >
            <button
              class="dropdown-trigger"
              type="button"
              aria-expanded={simpleDropdownOpen}
              onclick={() => (simpleDropdownOpen = !simpleDropdownOpen)}
            >
              <span>{selectedSimpleOptionLabel}</span>
              <svg
                class="dropdown-chevron"
                viewBox="0 0 12 12"
                aria-hidden="true"
              >
                <path d="M2 4l4 4 4-4" />
              </svg>
            </button>
            <div class="dropdown-list">
              {#each simpleOptions as option (option.id)}
                <button
                  class:selected={option.id === selectedSimpleOptionId}
                  class="dropdown-option"
                  type="button"
                  onclick={() => selectSimpleOption(option)}
                >
                  {t(option.labelKey, option.labelFallback)}
                </button>
              {/each}
            </div>
          </div>
        </div>
        <div class="field-stack">
          <span class="micro-label"
            >{t('components.dropdowns.searchable', 'Searchable')}</span
          >
          <div
            class:open={searchableDropdownOpen}
            class="s-dropdown showcase-search-dropdown"
          >
            <button
              class="s-dropdown-trigger"
              type="button"
              aria-expanded={searchableDropdownOpen}
              onclick={() => (searchableDropdownOpen = !searchableDropdownOpen)}
            >
              <span>{selectedModelLabel}</span>
              <svg
                class="dropdown-chevron"
                viewBox="0 0 12 12"
                aria-hidden="true"
              >
                <path d="M2 4l4 4 4-4" />
              </svg>
            </button>
            <div class="s-dropdown-panel showcase-search-panel">
              <label class="s-dropdown-search">
                <svg viewBox="0 0 12 12" aria-hidden="true">
                  <circle cx="5" cy="5" r="3.5" />
                  <path d="M8 8l2.5 2.5" />
                </svg>
                <input
                  bind:value={modelFilter}
                  type="text"
                  placeholder={t('components.dropdowns.filter', 'Filter…')}
                />
              </label>
              <div class="s-dropdown-options">
                {#each filteredModelOptions as option (option.id)}
                  <button
                    class:selected={option.id === selectedModelId}
                    class="s-dropdown-opt"
                    type="button"
                    onclick={() => selectModel(option)}
                  >
                    {t(option.labelKey, option.labelFallback)}
                  </button>
                {:else}
                  <div class="s-dropdown-empty">
                    {t('components.dropdowns.noMatches', 'No matches')}
                  </div>
                {/each}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-toggles-title">
      <h3 id="components-toggles-title" class="comp-section-title">
        {t('components.sections.toggles', 'Toggles')}
      </h3>
      <div class="comp-row toggle-demo-row">
        <div class="field-stack">
          <span class="micro-label"
            >{t('components.toggles.large', 'Large')}</span
          >
          <button
            class="toggle on"
            class:off={!largeToggleOn}
            type="button"
            role="switch"
            aria-checked={largeToggleOn}
            aria-label={t('components.toggles.largeOn', 'Large toggle on')}
            onclick={() => (largeToggleOn = !largeToggleOn)}
          >
            <span class="t-knob"></span>
          </button>
          <button
            class="toggle on"
            class:off={!largeToggleOff}
            type="button"
            role="switch"
            aria-checked={largeToggleOff}
            aria-label={t('components.toggles.largeOff', 'Large toggle off')}
            onclick={() => (largeToggleOff = !largeToggleOff)}
          >
            <span class="t-knob"></span>
          </button>
        </div>
        <div class="field-stack">
          <span class="micro-label"
            >{t('components.toggles.small', 'Small')}</span
          >
          <button
            class="tl-toggle on"
            class:off={!smallToggleOn}
            type="button"
            role="switch"
            aria-checked={smallToggleOn}
            aria-label={t('components.toggles.smallOn', 'Small toggle on')}
            onclick={() => (smallToggleOn = !smallToggleOn)}
          >
            <span class="t-knob"></span>
          </button>
          <button
            class="tl-toggle on"
            class:off={!smallToggleOff}
            type="button"
            role="switch"
            aria-checked={smallToggleOff}
            aria-label={t('components.toggles.smallOff', 'Small toggle off')}
            onclick={() => (smallToggleOff = !smallToggleOff)}
          >
            <span class="t-knob"></span>
          </button>
        </div>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-tooltips-title">
      <h3 id="components-tooltips-title" class="comp-section-title">
        {t('components.sections.tooltips', 'Tooltips (hover each icon)')}
      </h3>
      <div class="comp-row">
        <button
          class="icon-btn"
          type="button"
          data-tooltip={t('components.tooltips.attachFile', 'Attach file')}
          aria-label={t('components.tooltips.attachFile', 'Attach file')}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true"
            ><path
              d="M13 7l-5 5a3.5 3.5 0 0 1-5-5l5-5a2 2 0 0 1 3 3L6 10a.5.5 0 0 1-1-1l4.5-4.5"
            /></svg
          >
        </button>
        <button
          class="icon-btn"
          type="button"
          data-tooltip={t('components.tooltips.copy', 'Copy to clipboard')}
          aria-label={t('components.tooltips.copy', 'Copy to clipboard')}
        >
          <svg viewBox="0 0 14 14" aria-hidden="true"
            ><rect x="1" y="4" width="9" height="9" rx="1.5" /><path
              d="M4 4V2.5A1.5 1.5 0 0 1 5.5 1H11.5A1.5 1.5 0 0 1 13 2.5v6A1.5 1.5 0 0 1 11.5 10H10"
            /></svg
          >
        </button>
        <button
          class="icon-btn"
          type="button"
          data-tooltip={t('components.tooltips.delete', 'Delete message')}
          aria-label={t('components.tooltips.delete', 'Delete message')}
        >
          <svg viewBox="0 0 14 14" aria-hidden="true"
            ><path d="M2 3h10M5 3V2h4v1M4 3l.6 8h4.8L10 3" /></svg
          >
        </button>
        <button
          class="icon-btn"
          type="button"
          data-tooltip={t(
            'components.tooltips.regenerate',
            'Regenerate response',
          )}
          aria-label={t(
            'components.tooltips.regenerate',
            'Regenerate response',
          )}
        >
          <svg viewBox="0 0 14 14" aria-hidden="true"
            ><path d="M12 2v4H8" /><path d="M12 6A5 5 0 1 1 9.5 2.3" /></svg
          >
        </button>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-typography-title">
      <h3 id="components-typography-title" class="comp-section-title">
        {t('components.sections.typography', 'Typography')}
      </h3>
      <div class="type-stack">
        <div class="field-stack">
          <span class="micro-label"
            >{t(
              'components.typography.uiText',
              'IBM Plex Sans — UI text',
            )}</span
          >
          <span class="type-display"
            >{t(
              'components.typography.agentHeading',
              'Agent heading — 22px / 600',
            )}</span
          >
          <span class="type-heading"
            >{t(
              'components.typography.settingsPanel',
              'Settings panel — 20px / 600',
            )}</span
          >
          <span class="type-modal"
            >{t(
              'components.typography.modalTitle',
              'Modal title — 15px / 600',
            )}</span
          >
          <span class="type-message"
            >{t(
              'components.typography.messageBody',
              'Message body — 14px / 400',
            )}</span
          >
          <span class="type-body"
            >{t(
              'components.typography.bodyDefault',
              'Body default — 13.5px / 400',
            )}</span
          >
          <span class="type-nav"
            >{t('components.typography.navItem', 'Nav item — 13px / 500')}</span
          >
          <span class="type-desc"
            >{t(
              'components.typography.descriptionText',
              'Description text — 12.5px / 400',
            )}</span
          >
        </div>
        <div class="field-stack">
          <span class="micro-label"
            >{t(
              'components.typography.technicalText',
              'IBM Plex Mono — technical / code',
            )}</span
          >
          <span class="mono-13"
            >{t(
              'components.typography.modelName',
              'Model name — 13px / 400',
            )}</span
          >
          <span class="mono-12-5"
            >{t(
              'components.typography.settingsInputValue',
              'Settings input value — 12.5px / 400',
            )}</span
          >
          <span class="mono-12"
            >{t(
              'components.typography.toolNameArgs',
              'Tool fn name & args — 12px / 500',
            )}</span
          >
          <span class="mono-11-5"
            >{t(
              'components.typography.toastChipText',
              'Toast & chip text — 11.5px',
            )}</span
          >
          <span class="mono-10-5"
            >{t(
              'components.typography.sectionLabel',
              'SECTION LABEL — 10.5px / 500 uppercase',
            )}</span
          >
        </div>
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-chips-title">
      <h3 id="components-chips-title" class="comp-section-title">
        {t('components.sections.statusChips', 'Status chips')}
      </h3>
      <div class="comp-row">
        <span class="chip chip-green">{t('status.connected', 'Connected')}</span
        >
        <span class="chip chip-amber"
          >{t('status.activeRun', 'active run')}</span
        >
        <span class="chip chip-orange">{t('status.medium', 'medium')}</span>
        <span class="chip chip-red"
          >{t('status.notReachable', 'Not reachable')}</span
        >
        <span class="chip chip-inactive"
          >{t('status.inactive', 'Inactive')}</span
        >
      </div>
    </section>

    <section class="comp-section" aria-labelledby="components-chat-title">
      <h3 id="components-chat-title" class="comp-section-title">
        {t('components.sections.chatMessages', 'Chat — messages')}
      </h3>
      <div class="chat-showcase">
        <div class="date-sep showcase-date">
          {t('components.chatShowcase.date', 'April 20, 2026')}
        </div>
        <div class="msg user showcase-msg">
          <div class="msg-header">
            <div class="msg-avatar">{t('chat.role.userAvatar', 'Y')}</div>
            <span class="msg-author"
              >{t('chat.role.user', 'You').toUpperCase()}</span
            >
            <span class="msg-timestamp">
              {t('components.chatShowcase.timestamp', '12:36 PM')}
            </span>
          </div>
          <div class="msg-content">
            <div class="msg-body-text">
              {t(
                'components.chatShowcase.userMessage',
                'Hey, can you check if the server is running?',
              )}
            </div>
          </div>
        </div>
        <div class="msg assistant showcase-msg">
          <div class="msg-header">
            <div class="msg-avatar">{t('chat.role.assistantAvatar', 'A')}</div>
            <span class="msg-author">
              {t('chat.role.assistant', 'Assistant').toUpperCase()}
            </span>
            <span class="msg-timestamp">
              {t('components.chatShowcase.timestamp', '12:36 PM')}
            </span>
            <span class="msg-meta-extra"
              >{t(
                'components.chatShowcase.assistantMeta',
                '· 2 iterations · 8.3s',
              )}</span
            >
          </div>
          <div class="msg-content">
            <div class:open={thinkingOpen} class="reasoning-block">
              <button
                class="reasoning-header"
                type="button"
                onclick={() => (thinkingOpen = !thinkingOpen)}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true"
                  ><path
                    d="M8 2a4 4 0 0 0-4 4c0 1.5.8 2.8 2 3.5V11h4V9.5A4 4 0 0 0 12 6a4 4 0 0 0-4-4z"
                  /><path d="M6 13h4" /></svg
                >
                {t('chat.event.thinking', 'Thinking').toUpperCase()}
                <svg class="r-chevron" viewBox="0 0 16 16" aria-hidden="true"
                  ><path d="M4 6l4 4 4-4" /></svg
                >
              </button>
              <div class="reasoning-body">
                {t(
                  'components.chatShowcase.thinking',
                  'I should call a placeholder tool to demonstrate how reasoning appears in the timeline.',
                )}
              </div>
            </div>
            <div class:open={toolEventOpen} class="tool-event">
              <button
                class="tool-event-line"
                type="button"
                onclick={() => (toolEventOpen = !toolEventOpen)}
              >
                <span class="te-dot done">●</span>
                <span class="te-fn">
                  {t('components.chatShowcase.toolName', 'showcase_tool')}
                </span>
                <span class="te-arg">
                  {t('components.chatShowcase.toolArg', '(sample)')}
                </span>
                <span class="te-time">
                  {t('components.chatShowcase.toolTime', '✓ 3ms')}
                </span>
              </button>
              <div class="tool-event-body">
                <div class="teb-row">
                  <span class="teb-label">{t('chat.toolArgs', 'Args')}</span
                  ><span class="teb-code">
                    {t(
                      'components.chatShowcase.toolArgsJson',
                      '{"name":"sample"}',
                    )}
                  </span>
                </div>
                <div class="teb-row">
                  <span class="teb-label"
                    >{t('chat.toolResultLabel', 'Result')}</span
                  ><span class="teb-code">
                    {t(
                      'components.chatShowcase.toolResultJson',
                      '{"status":"sample","duration_ms":3}',
                    )}
                  </span>
                </div>
              </div>
            </div>
            <div class="msg-body-text">
              {t(
                'components.chatShowcase.assistantMessage',
                'This is placeholder timeline content for the Toasted component showcase.',
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>

  <div class="toast-stack" aria-live="polite">
    {#each toasts as toast (toast.id)}
      <div class={`toast ${toast.type}`}>
        <div class="toast-body">
          <div class="toast-title">{toast.title}</div>
          <div class="toast-msg">{toast.message}</div>
        </div>
        <button
          class="toast-close"
          type="button"
          aria-label={t('common.dismiss', 'Dismiss')}
          onclick={() => dismissToast(toast.id)}
        >
          ×
        </button>
      </div>
    {/each}
  </div>
</section>

<style>
  .btn-new svg,
  .icon-btn svg,
  .send-btn svg,
  .dropdown-chevron,
  .s-dropdown-search svg,
  .reasoning-header svg {
    width: 14px;
    height: 14px;
  }

  .btn-new svg {
    width: 11px;
    height: 11px;
  }

  .icon-btn--small {
    width: 24px;
    height: 24px;
  }

  .showcase-code {
    max-width: 560px;
  }

  .showcase-empty {
    min-height: 160px;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--surface);
  }

  .showcase-empty svg {
    width: 32px;
    height: 32px;
  }

  .inline-confirm {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .inline-confirm-label {
    color: var(--text-med);
    font-size: 12.5px;
  }

  .input-showcase,
  .field-stack,
  .type-stack {
    display: flex;
    flex-direction: column;
  }

  .input-showcase {
    max-width: 360px;
    gap: 10px;
  }

  .field-stack {
    gap: 6px;
  }

  .comp-row--top {
    align-items: flex-start;
    gap: 24px;
  }

  .micro-label {
    color: var(--text-lo);
    font-family: var(--font-mono);
    font-size: 10px;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .showcase-dropdown {
    min-width: 160px;
  }

  .showcase-search-dropdown {
    width: 260px;
  }

  .showcase-search-panel {
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    left: 0;
  }

  .dropdown-trigger,
  .s-dropdown-trigger,
  .dropdown-option,
  .s-dropdown-opt {
    width: 100%;
    border: 0;
    text-align: left;
  }

  .dropdown-trigger,
  .s-dropdown-trigger {
    border: 1px solid var(--border-2);
  }

  .dropdown-option,
  .s-dropdown-opt {
    display: block;
    background: transparent;
  }

  .s-dropdown-search svg {
    flex-shrink: 0;
    opacity: 0.4;
  }

  .toggle-demo-row {
    align-items: flex-start;
    gap: 28px;
  }

  .toggle.off,
  .tl-toggle.off {
    border-color: var(--border-2);
    background: var(--surface-3);
  }

  .toggle.off .t-knob,
  .tl-toggle.off .t-knob {
    left: 2px;
  }

  .type-stack {
    gap: 18px;
  }

  .type-display {
    color: var(--text-hi);
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.03em;
  }

  .type-heading {
    color: var(--text-hi);
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
  }

  .type-modal {
    color: var(--text-hi);
    font-size: 15px;
    font-weight: 600;
  }

  .type-message {
    color: var(--text-hi);
    font-size: 14px;
  }

  .type-body {
    color: var(--text-hi);
    font-size: 13.5px;
  }

  .type-nav {
    color: var(--text-hi);
    font-size: 13px;
    font-weight: 500;
  }

  .type-desc {
    color: var(--text-med);
    font-size: 12.5px;
  }

  .mono-13,
  .mono-12-5,
  .mono-12,
  .mono-11-5,
  .mono-10-5 {
    font-family: var(--font-mono);
  }

  .mono-13 {
    color: var(--text-hi);
    font-size: 13px;
  }

  .mono-12-5 {
    color: var(--text-hi);
    font-size: 12.5px;
  }

  .mono-12 {
    color: var(--text-med);
    font-size: 12px;
    font-weight: 500;
  }

  .mono-11-5 {
    color: var(--text-med);
    font-size: 11.5px;
  }

  .mono-10-5 {
    color: var(--text-lo);
    font-size: 10.5px;
  }

  .chip-inactive {
    color: var(--text-lo);
    background: var(--surface-3);
  }

  .chat-showcase {
    display: flex;
    max-width: 700px;
    flex-direction: column;
    gap: 4px;
  }

  .showcase-date {
    padding: 0 0 16px;
  }

  .showcase-msg {
    padding: 4px 0;
  }

  .reasoning-header,
  .tool-event-line {
    border: 0;
    background: transparent;
    text-align: left;
  }

  .reasoning-header svg:first-child {
    opacity: 0.4;
  }

  .toast-stack {
    position: fixed;
  }
</style>
