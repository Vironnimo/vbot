<script>
  import Button from './ui/Button.svelte';
  import { listSessions } from '$lib/api.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import {
    applySessionList,
    createSessionListState,
    selectSession,
    sessionDisplayName,
  } from '$lib/sessionListView.js';

  let {
    agentId = '',
    currentSessionId = '',
    agentCurrentSessionId = '',
    onSessionSelected = () => {},
  } = $props();

  const timestampFormatter = new Intl.DateTimeFormat(activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: 'short',
  });

  let sessionState = $state(createSessionListState());

  let loadedAgentId = '';
  let loadVersion = 0;

  $effect(() => {
    const normalizedAgentId = asText(agentId);
    if (normalizedAgentId === loadedAgentId) {
      return;
    }

    loadedAgentId = normalizedAgentId;

    if (!normalizedAgentId) {
      sessionState = createSessionListState();
      return;
    }

    loadSessions(normalizedAgentId);
  });

  $effect(() => {
    const normalizedCurrentSessionId = asText(currentSessionId);

    if (!normalizedCurrentSessionId) {
      return;
    }

    const hasCurrentSession = sessionState.sessions.some(
      (session) => session.id === normalizedCurrentSessionId,
    );

    if (!hasCurrentSession) {
      return;
    }

    if (sessionState.selectedSessionId === normalizedCurrentSessionId) {
      return;
    }

    sessionState = selectSession(sessionState, normalizedCurrentSessionId);
  });

  const loadSessions = async (targetAgentId = asText(agentId)) => {
    if (!targetAgentId) {
      sessionState = createSessionListState();
      return;
    }

    const requestVersion = ++loadVersion;
    sessionState = {
      ...sessionState,
      loading: true,
      error: null,
    };

    try {
      const result = await listSessions(targetAgentId);
      if (requestVersion !== loadVersion) {
        return;
      }

      sessionState = applySessionList(sessionState, result?.sessions ?? []);
      const normalizedCurrentSessionId = asText(currentSessionId);
      if (normalizedCurrentSessionId) {
        sessionState = selectSession(sessionState, normalizedCurrentSessionId);
      }
    } catch (error) {
      if (requestVersion !== loadVersion) {
        return;
      }

      sessionState = {
        ...sessionState,
        loading: false,
        error: error.message,
      };
    }
  };

  const handleSelectSession = (sessionId) => {
    sessionState = selectSession(sessionState, sessionId);
    onSessionSelected?.(sessionId);
  };

  const formatTimestamp = (value) => {
    const normalizedValue = asText(value);
    if (!normalizedValue) {
      return t('common.unknown', 'Unknown');
    }

    const parsedValue = Date.parse(normalizedValue);
    if (Number.isNaN(parsedValue)) {
      return normalizedValue;
    }

    return timestampFormatter.format(new Date(parsedValue));
  };

  const resolvePlatformLabel = (platform) => {
    if (platform === 'telegram') {
      return t('sessions.platform_telegram', 'Telegram');
    }
    return platform;
  };

  function asText(value) {
    if (value === null || value === undefined) {
      return '';
    }
    const normalizedValue = String(value).trim();
    return normalizedValue;
  }
</script>

<aside class="session-drawer" aria-label={t('sessions.title', 'Sessions')}>
  <div class="session-drawer__header">
    <h3 class="session-drawer__title">{t('sessions.title', 'Sessions')}</h3>
    <Button
      variant="secondary"
      class="session-drawer__refresh"
      disabled={sessionState.loading || !agentId}
      onClick={() => loadSessions()}
    >
      {t('common.refresh', 'Refresh')}
    </Button>
  </div>

  {#if sessionState.error}
    <p class="session-drawer__state session-drawer__state--error">
      {sessionState.error}
    </p>
  {:else if sessionState.loading && sessionState.sessions.length === 0}
    <p class="session-drawer__state">
      {t('sessions.loading', 'Loading sessions…')}
    </p>
  {:else if sessionState.sessions.length === 0}
    <p class="session-drawer__state">
      {t('sessions.no_sessions', 'No sessions found for this agent.')}
    </p>
  {:else}
    <ul class="session-drawer__list">
      {#each sessionState.sessions as session (session.id)}
        <li class="session-row">
          <button
            type="button"
            class:session-row__select--active={sessionState.selectedSessionId ===
              session.id}
            class="session-row__select"
            onclick={() => handleSelectSession(session.id)}
          >
            <div class="session-row__heading">
              <p class="session-row__name">
                {session.display_name || sessionDisplayName(session)}
              </p>
              {#if session.id === asText(agentCurrentSessionId)}
                <span class="session-row__badge session-row__badge--current">
                  {t('sessions.current', 'Current')}
                </span>
              {/if}
              {#if session.platform}
                <span class="session-row__badge">
                  {#if session.platform === 'telegram'}
                    <svg viewBox="0 0 18 18" aria-hidden="true">
                      <path
                        d="M15.36 3.27c.39-.15.77.2.67.61l-1.94 9.14c-.07.34-.45.5-.74.31l-3.16-2.13-1.62 1.57c-.22.22-.6.11-.67-.2l-.52-2.41 6.72-5.91c.14-.12-.04-.35-.2-.24L5.6 9.04 2.5 7.8c-.34-.13-.35-.6-.02-.75l12.88-3.78z"
                      />
                    </svg>
                  {/if}
                  <span>{resolvePlatformLabel(session.platform)}</span>
                </span>
              {/if}
              {#if session.is_subagent_session}
                <span class="session-row__badge session-row__badge--subagent">
                  {t('chat.subagent.label', 'Sub-agent')}
                </span>
              {/if}
            </div>
            <p class="session-row__meta">
              {t('sessions.last_active', 'Last active')}:
              {formatTimestamp(session.last_active_at ?? session.created_at)}
            </p>
            {#if session.source_channel_id}
              <p class="session-row__meta session-row__meta--mono">
                {t('sessions.source_channel', 'Source channel')}:
                {session.source_channel_id}
              </p>
            {/if}
            {#if session.subagent_parent}
              <p class="session-row__meta session-row__meta--mono">
                {t('sessions.subagent_parent', 'Parent')}:
                {session.subagent_parent.agent_id}/{session.subagent_parent
                  .session_id}
              </p>
            {/if}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</aside>

<style>
  .session-drawer {
    display: flex;
    width: 295px;
    min-width: 295px;
    flex-direction: column;
    border-right: 1px solid var(--border);
    background: var(--surface);
    overflow: hidden;
  }

  .session-drawer__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
  }

  .session-drawer__title {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  :global(.session-drawer__refresh) {
    padding: 4px 10px;
    font-size: 12px;
  }

  .session-drawer__list {
    margin: 0;
    padding: 12px 10px 14px;
    list-style: none;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .session-row {
    flex: 0 0 auto;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
    overflow: hidden;
    box-shadow: inset 3px 0 0 transparent;
    transition:
      border-color 150ms ease,
      box-shadow 150ms ease,
      background 150ms ease;
  }

  .session-row:has(.session-row__select--active) {
    border-color: rgba(232, 135, 10, 0.46);
    box-shadow: inset 3px 0 0 var(--accent);
  }

  .session-row__select {
    width: 100%;
    min-height: 64px;
    border: 0;
    padding: 12px 12px 11px 14px;
    text-align: left;
    background: transparent;
    color: var(--text-hi);
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 7px;
    line-height: 1.35;
    transition:
      background 150ms ease,
      border-color 150ms ease;
  }

  .session-row__select:hover,
  .session-row__select:focus-visible {
    outline: none;
    background: rgba(232, 135, 10, 0.08);
  }

  .session-row__select--active {
    background: rgba(232, 135, 10, 0.12);
    box-shadow: inset 2px 0 0 var(--accent);
  }

  .session-row__heading {
    display: flex;
    min-width: 0;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .session-row__name {
    min-width: 0;
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 600;
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-row__badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border: 1px solid rgba(232, 135, 10, 0.28);
    border-radius: 999px;
    padding: 2px 7px;
    color: var(--accent);
    background: rgba(232, 135, 10, 0.08);
    font-family: var(--font-mono);
    font-size: 10.5px;
    line-height: 1;
  }

  .session-row__badge--current {
    border-color: rgba(74, 222, 128, 0.28);
    background: rgba(74, 222, 128, 0.1);
    color: var(--green);
  }

  .session-row__badge--subagent {
    border-color: rgba(91, 141, 239, 0.32);
    background: rgba(91, 141, 239, 0.14);
    color: #8fb4ff;
  }

  .session-row__badge svg {
    width: 10px;
    height: 10px;
    fill: currentColor;
  }

  .session-row__meta {
    margin: 0;
    color: var(--text-med);
    font-size: 11.5px;
    line-height: 1.35;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-row__meta--mono {
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .session-drawer__state {
    margin: 0;
    padding: 10px 12px;
    color: var(--text-med);
    font-size: 12px;
  }

  .session-drawer__state--error {
    color: var(--red);
  }

  @media (max-width: 640px) {
    .session-drawer {
      width: 100%;
      min-width: 0;
      border-right: 0;
      border-bottom: 1px solid var(--border);
      max-height: 46%;
    }
  }
</style>
