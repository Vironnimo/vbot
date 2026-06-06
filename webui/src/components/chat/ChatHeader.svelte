<script>
  import { isRunActive } from '$lib/chatState.js';
  import { t } from '$lib/i18n.js';

  let {
    agents = [],
    selectedAgentId = '',
    loadingAgents = false,
    activeAgent = null,
    activeSessionState = null,
    showSessionDrawer = false,
    cancellingRun = false,
    creatingSession = false,
    newSessionBlocked = false,
    wakewordStatus = { enabled: false, state: 'off' },
    desktopCapabilities = null,
    onSelectAgent = () => {},
    onToggleSessionDrawer = () => {},
    onCancelRun = () => {},
    onNewSession = () => {},
    onNavigateToVoiceSettings = () => {},
  } = $props();

  let tokenBadgeText = $derived.by(() =>
    formatTokenBadge(activeSessionState?.usage, activeAgent?.context_window),
  );
  let micDotClass = $derived(computeMicDotClass(wakewordStatus));
  let micTooltip = $derived(computeMicTooltip(wakewordStatus));
  let micVisible = $derived(Boolean(desktopCapabilities?.wakeword));

  function formatTokenBadge(usage, contextWindow) {
    const numberFormat = new Intl.NumberFormat();

    if (usage) {
      const inputTokens = Number.isFinite(usage.input_tokens)
        ? usage.input_tokens
        : 0;
      const outputTokens = Number.isFinite(usage.output_tokens)
        ? usage.output_tokens
        : 0;
      const tokensFormatted = numberFormat.format(inputTokens + outputTokens);
      const estimated = usage.estimated === true;

      if (contextWindow != null) {
        const contextFormatted = numberFormat.format(contextWindow);
        return estimated
          ? t('chat.tokenBadgeEstimated', '~{tokens} / {context} tok', {
              tokens: tokensFormatted,
              context: contextFormatted,
            })
          : t('chat.tokenBadge', '{tokens} / {context} tok', {
              tokens: tokensFormatted,
              context: contextFormatted,
            });
      }
      return estimated
        ? t('chat.tokenBadgeEstimatedNoContext', '~{tokens} tok', {
            tokens: tokensFormatted,
          })
        : t('chat.tokenBadgeNoContext', '{tokens} tok', {
            tokens: tokensFormatted,
          });
    }
    if (contextWindow != null) {
      return t('chat.tokenBadgeNoUsage', '— / {context} tok', {
        context: numberFormat.format(contextWindow),
      });
    }
    return '';
  }

  function computeMicDotClass(status) {
    if (!status?.enabled) {
      return 'mic-dot--off';
    }
    switch (status.state) {
      case 'listening':
      case 'wakeword_detected':
        return 'mic-dot--listening';
      case 'recording':
        return 'mic-dot--recording';
      case 'transcribing':
      case 'sending':
        return 'mic-dot--processing';
      case 'error':
        return 'mic-dot--error';
      default:
        return 'mic-dot--off';
    }
  }

  function computeMicTooltip(status) {
    if (!status?.enabled) {
      return t('voice.mic.tooltip.off', 'Wakeword disabled');
    }
    switch (status.state) {
      case 'listening':
        return t('voice.mic.tooltip.listening', 'Listening for wakeword');
      case 'wakeword_detected':
        return t('voice.mic.tooltip.detected', 'Wakeword detected');
      case 'recording':
        return t('voice.mic.tooltip.recording', 'Recording voice command');
      case 'transcribing':
      case 'sending':
        return t('voice.mic.tooltip.processing', 'Processing voice command');
      case 'error':
        return t('voice.mic.tooltip.error', 'Voice error');
      default:
        return t('voice.mic.tooltip.off', 'Wakeword disabled');
    }
  }
</script>

<header class="chat-header">
  <h2 id="chat-title" class="chat-title">{t('chat.title', 'Chat')}</h2>
  <div class="agent-tabs" aria-label={t('chat.selectAgent', 'Select agent')}>
    {#if agents.length > 0}
      {#each agents as agent (agent.id)}
        <button
          type="button"
          class:active={agent.id === selectedAgentId}
          class="agent-tab"
          disabled={loadingAgents}
          onclick={() => onSelectAgent(agent.id)}
        >
          <span class="tab-indicator"></span>
          <span>{agent.name}</span>
        </button>
      {/each}
    {:else}
      <span class="agent-tab agent-tab--empty">
        <span class="tab-indicator"></span>
        {t('chat.noAgents', 'No agents are available yet.')}
      </span>
    {/if}
  </div>
  <div class="header-right">
    {#if micVisible}
      <button
        type="button"
        class="mic-indicator"
        title={micTooltip}
        aria-label={micTooltip}
        onclick={onNavigateToVoiceSettings}
      >
        <span class="mic-dot {micDotClass}" aria-hidden="true"></span>
      </button>
    {/if}
    {#if tokenBadgeText}
      <span class="token-badge">{tokenBadgeText}</span>
    {/if}
    <button
      type="button"
      class:chat-sessions-toggle--active={showSessionDrawer}
      class="btn-outline chat-sessions-toggle"
      onclick={onToggleSessionDrawer}
      disabled={!activeAgent}
    >
      {showSessionDrawer
        ? t('sessions.hide', 'Hide sessions')
        : t('sessions.title', 'Sessions')}
    </button>
    {#if activeSessionState && isRunActive(activeSessionState)}
      <button
        type="button"
        class="btn-outline btn-dang"
        disabled={cancellingRun}
        onclick={onCancelRun}
      >
        {cancellingRun
          ? t('cancel.cancelling', 'Cancelling run…')
          : t('chat.cancelRun', 'Cancel run')}
      </button>
    {/if}
    <button
      type="button"
      class="btn-new"
      disabled={!activeAgent || newSessionBlocked || creatingSession}
      title={newSessionBlocked
        ? t(
            'chat.newSessionBlocked',
            'A new session can be started after the current run finishes.',
          )
        : undefined}
      onclick={onNewSession}
    >
      <svg viewBox="0 0 14 14" aria-hidden="true">
        <path d="M7 1v12M1 7h12" />
      </svg>
      {creatingSession
        ? t('common.loading', 'Loading…')
        : t('chat.newSession', 'New Session')}
    </button>
  </div>
</header>

<style>
  .chat-header {
    display: flex;
    height: 50px;
    flex-shrink: 0;
    align-items: center;
    gap: 8px;
    padding: 0 20px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .chat-title {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
  }

  .agent-tabs {
    display: flex;
    min-width: 0;
    height: 100%;
    flex: 1;
    align-items: stretch;
    gap: 2px;
    overflow-x: auto;
  }

  .agent-tab {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 7px;
    padding: 0 14px;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--text-lo);
    background: transparent;
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 500;
    white-space: nowrap;
    transition:
      border-color 150ms ease,
      color 150ms ease;
  }

  .agent-tab:hover,
  .agent-tab:focus-visible {
    color: var(--text-med);
    outline: none;
  }

  .agent-tab.active {
    border-bottom-color: var(--accent);
    color: var(--accent);
  }

  .agent-tab--empty {
    cursor: default;
  }

  .tab-indicator {
    width: 5px;
    height: 5px;
  }

  .header-right {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    gap: 10px;
  }

  .token-badge {
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: var(--r-sm);
    color: var(--text-lo);
    background: var(--surface-2);
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .mic-indicator {
    display: inline-flex;
    width: 28px;
    height: 28px;
    flex-shrink: 0;
    align-items: center;
    justify-content: center;
    padding: 0;
    border: none;
    border-radius: 50%;
    background: transparent;
    cursor: pointer;
    transition: background 0.15s;
  }

  .mic-indicator:hover {
    background: var(--surface-2);
  }

  .mic-dot {
    display: block;
    width: 8px;
    height: 8px;
    flex-shrink: 0;
    border-radius: 50%;
  }

  .mic-dot--off {
    background: var(--text-lo, #5e4c38);
  }

  .mic-dot--listening {
    animation: mic-pulse 1.6s ease-in-out infinite;
    background: var(--green, #4ade80);
  }

  .mic-dot--recording {
    background: var(--amber, #f59e0b);
  }

  .mic-dot--processing {
    animation: mic-spin 1s linear infinite;
    background: var(--accent, #e8870a);
  }

  .mic-dot--error {
    background: var(--red, #fc8181);
  }

  .chat-sessions-toggle--active {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(232, 135, 10, 0.08);
  }

  .btn-new svg {
    width: 12px;
    height: 12px;
  }

  @keyframes mic-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
  }

  @keyframes mic-spin {
    0% {
      opacity: 1;
    }
    25% {
      opacity: 0.5;
    }
    50% {
      opacity: 0.2;
    }
    75% {
      opacity: 0.5;
    }
    100% {
      opacity: 1;
    }
  }

  @media (max-width: 760px) {
    .chat-header {
      height: auto;
      flex-wrap: wrap;
      padding: 10px 14px;
    }

    .agent-tabs {
      order: 2;
      width: 100%;
      height: 38px;
      flex-basis: 100%;
    }

    .header-right {
      margin-left: auto;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .token-badge {
      display: none;
    }
  }
</style>
