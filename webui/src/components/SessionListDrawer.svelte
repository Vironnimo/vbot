<script>
  import { linkSessionToChannel, listSessions } from '$lib/api.js';
  import { t } from '$lib/i18n.js';
  import {
    applySessionList,
    createSessionListState,
    selectSession,
    sessionDisplayName,
  } from '$lib/sessionListView.js';

  let {
    agentId = '',
    currentSessionId = '',
    onSessionSelected = () => {},
  } = $props();

  const timestampFormatter = new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });

  let sessionState = $state(createSessionListState());
  let openLinkSessionId = $state('');
  let linkChannelId = $state('');
  let linkPlatformConvId = $state('');
  let linkLoading = $state(false);
  let linkError = $state('');
  let linkNotice = $state('');

  let loadedAgentId = '';
  let loadVersion = 0;

  $effect(() => {
    const normalizedAgentId = asText(agentId);
    if (normalizedAgentId === loadedAgentId) {
      return;
    }

    loadedAgentId = normalizedAgentId;
    resetLinkForm();

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

  const openLinkForm = (sessionId) => {
    openLinkSessionId = sessionId;
    linkChannelId = '';
    linkPlatformConvId = '';
    linkError = '';
    linkNotice = '';
  };

  const closeLinkForm = () => {
    openLinkSessionId = '';
    linkChannelId = '';
    linkPlatformConvId = '';
    linkError = '';
  };

  const resetLinkForm = () => {
    closeLinkForm();
    linkNotice = '';
  };

  const submitLink = async (sessionId) => {
    const normalizedAgentId = asText(agentId);
    const normalizedChannelId = asText(linkChannelId);
    const normalizedPlatformConvId = asText(linkPlatformConvId);

    if (!normalizedAgentId || !sessionId) {
      return;
    }

    if (!normalizedChannelId || !normalizedPlatformConvId) {
      linkError = t(
        'errors.validation',
        'Check the highlighted fields and try again.',
      );
      return;
    }

    linkLoading = true;
    linkError = '';
    linkNotice = '';

    try {
      await linkSessionToChannel(
        normalizedAgentId,
        sessionId,
        normalizedChannelId,
        normalizedPlatformConvId,
      );
      linkNotice = t('sessions.link_success', 'Session linked to channel.');
      closeLinkForm();
      await loadSessions(normalizedAgentId);
    } catch (error) {
      linkError = error.message;
    } finally {
      linkLoading = false;
    }
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
    <button
      type="button"
      class="btn-outline session-drawer__refresh"
      disabled={sessionState.loading || !agentId}
      onclick={() => loadSessions()}
    >
      {t('common.refresh', 'Refresh')}
    </button>
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
                {session.display_name ?? sessionDisplayName(session)}
              </p>
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
          </button>

          {#if !session.is_channel_session}
            <div class="session-row__link-block">
              {#if openLinkSessionId === session.id}
                <form
                  class="session-row__link-form"
                  onsubmit={(event) => {
                    event.preventDefault();
                    submitLink(session.id);
                  }}
                >
                  <label
                    class="session-row__label"
                    for={`channel-id-${session.id}`}
                  >
                    {t('sessions.link_channel_id', 'Channel ID')}
                  </label>
                  <input
                    id={`channel-id-${session.id}`}
                    class="s-input"
                    name="channel-id"
                    bind:value={linkChannelId}
                    placeholder={t('sessions.link_channel_id', 'Channel ID')}
                    autocomplete="off"
                  />

                  <label
                    class="session-row__label"
                    for={`platform-conv-id-${session.id}`}
                  >
                    {t(
                      'sessions.link_platform_conv_id',
                      'Platform conversation ID',
                    )}
                  </label>
                  <input
                    id={`platform-conv-id-${session.id}`}
                    class="s-input"
                    name="platform-conv-id"
                    bind:value={linkPlatformConvId}
                    placeholder={t(
                      'sessions.link_platform_conv_id',
                      'Platform conversation ID',
                    )}
                    autocomplete="off"
                  />

                  <div class="session-row__link-actions">
                    <button
                      type="submit"
                      class="btn-new"
                      disabled={linkLoading}
                    >
                      {linkLoading
                        ? t('common.loading', 'Loading…')
                        : t('sessions.link_confirm', 'Link session')}
                    </button>
                    <button
                      type="button"
                      class="btn-outline"
                      disabled={linkLoading}
                      onclick={closeLinkForm}
                    >
                      {t('common.cancel', 'Cancel')}
                    </button>
                  </div>
                </form>
              {:else}
                <button
                  type="button"
                  class="btn-outline session-row__link-toggle"
                  onclick={() => openLinkForm(session.id)}
                >
                  {t('sessions.link_to_channel', 'Link to channel')}
                </button>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if linkError}
    <p class="session-drawer__state session-drawer__state--error">
      {linkError}
    </p>
  {/if}

  {#if linkNotice}
    <p class="session-drawer__state session-drawer__state--notice">
      {linkNotice}
    </p>
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

  .session-drawer__refresh {
    padding: 4px 10px;
    font-size: 12px;
  }

  .session-drawer__list {
    margin: 0;
    padding: 10px;
    list-style: none;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .session-row {
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
    overflow: hidden;
  }

  .session-row__select {
    width: 100%;
    border: 0;
    padding: 10px;
    text-align: left;
    background: transparent;
    color: var(--text-hi);
    display: flex;
    flex-direction: column;
    gap: 6px;
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
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .session-row__name {
    margin: 0;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 12.5px;
    font-weight: 500;
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

  .session-row__badge svg {
    width: 10px;
    height: 10px;
    fill: currentColor;
  }

  .session-row__meta {
    margin: 0;
    color: var(--text-med);
    font-size: 11.5px;
  }

  .session-row__meta--mono {
    font-family: var(--font-mono);
    font-size: 11px;
  }

  .session-row__link-block {
    padding: 0 10px 10px;
  }

  .session-row__link-toggle {
    width: 100%;
  }

  .session-row__link-form {
    display: grid;
    gap: 6px;
  }

  .session-row__label {
    color: var(--text-med);
    font-family: var(--font-mono);
    font-size: 10.5px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .session-row__link-actions {
    display: flex;
    gap: 6px;
    margin-top: 4px;
  }

  .session-row__link-actions :global(button) {
    flex: 1;
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

  .session-drawer__state--notice {
    color: var(--green);
  }

  @media (max-width: 760px) {
    .session-drawer {
      width: 100%;
      min-width: 0;
      border-right: 0;
      border-bottom: 1px solid var(--border);
      max-height: 46%;
    }
  }
</style>
