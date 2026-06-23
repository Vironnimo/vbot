<script>
  import Button from './ui/Button.svelte';
  import { listSessions, renameSession } from '$lib/api.js';
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
    // Bumped by ChatView on `resource_changed(kind:"sessions")` so a new or
    // switched session created in another window appears here without the user
    // pressing Refresh.
    reloadToken = 0,
    onSessionSelected = () => {},
  } = $props();

  const timestampFormatter = new Intl.DateTimeFormat(activeLocaleTag(), {
    dateStyle: 'medium',
    timeStyle: 'short',
  });

  let sessionState = $state(createSessionListState());

  // Row-action state: which row's "…" menu is open, which row is being renamed
  // inline, the draft title, and any rename error. Only ever one of each at a
  // time — opening a menu or starting an edit on another row supersedes.
  let openMenuSessionId = $state(null);
  let editingSessionId = $state(null);
  let editValue = $state('');
  let renameError = $state(null);
  let renameSaving = $state(false);

  const SESSION_TITLE_MAX_LENGTH = 200;

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

  // Reload the list when another window creates/switches a session
  // (`resource_changed(kind:"sessions")`, forwarded by ChatView). The viewed
  // conversation stays put — only the list refreshes.
  let lastReloadToken = null;
  $effect(() => {
    if (lastReloadToken === null) {
      lastReloadToken = reloadToken;
      return;
    }
    if (reloadToken !== lastReloadToken) {
      lastReloadToken = reloadToken;
      loadSessions();
    }
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

  const toggleMenu = (sessionId) => {
    openMenuSessionId = openMenuSessionId === sessionId ? null : sessionId;
  };

  const closeMenu = () => {
    openMenuSessionId = null;
  };

  // Enter inline-rename for a row. Seeds the field with the existing custom
  // title (empty when the row currently shows an automatic label, so the user
  // names it fresh).
  const startRename = (session) => {
    closeMenu();
    editingSessionId = session.id;
    editValue = session.title ?? '';
    renameError = null;
  };

  const cancelRename = () => {
    editingSessionId = null;
    editValue = '';
    renameError = null;
  };

  const submitRename = async () => {
    const sessionId = editingSessionId;
    const targetAgentId = asText(agentId);
    if (!sessionId || !targetAgentId || renameSaving) {
      return;
    }

    renameSaving = true;
    renameError = null;
    try {
      await renameSession(targetAgentId, sessionId, editValue);
      editingSessionId = null;
      editValue = '';
      // Re-fetch so the row reflects the server-normalized title (and the
      // fallback label when the name was cleared).
      await loadSessions(targetAgentId);
    } catch (error) {
      renameError =
        error.message ||
        t('sessions.rename_error', 'The session could not be renamed.');
    } finally {
      renameSaving = false;
    }
  };

  const handleRenameKeydown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      submitRename();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      cancelRename();
    }
  };

  // Close an open row menu on an outside click or Escape, mirroring the
  // Dropdown primitive. Clicks inside any row's action area (trigger or menu)
  // are left to the buttons' own handlers.
  const handleDocumentMouseDown = (event) => {
    if (openMenuSessionId === null) {
      return;
    }
    if (
      event.target instanceof Element &&
      event.target.closest('.session-row__actions')
    ) {
      return;
    }
    closeMenu();
  };

  const handleDocumentKeyDown = (event) => {
    if (event.key === 'Escape') {
      closeMenu();
    }
  };

  // Focus (and select) the inline rename field as soon as it mounts.
  const autofocusRename = (node) => {
    node.focus();
    node.select();
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

<svelte:document
  onmousedown={handleDocumentMouseDown}
  onkeydown={handleDocumentKeyDown}
/>

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
        <li
          class="session-row"
          class:session-row--editing={editingSessionId === session.id}
        >
          {#if editingSessionId === session.id}
            <div class="session-row__edit">
              <input
                class="session-row__edit-input"
                type="text"
                value={editValue}
                maxlength={SESSION_TITLE_MAX_LENGTH}
                placeholder={t('sessions.rename_placeholder', 'Session name')}
                aria-label={t('sessions.rename_label', 'Rename session')}
                disabled={renameSaving}
                oninput={(event) => (editValue = event.currentTarget.value)}
                onkeydown={handleRenameKeydown}
                use:autofocusRename
              />
              {#if renameError}
                <p class="session-row__edit-error" role="alert">
                  {renameError}
                </p>
              {/if}
            </div>
          {:else}
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
            <div class="session-row__actions">
              <button
                type="button"
                class="session-row__menu-trigger"
                class:session-row__menu-trigger--open={openMenuSessionId ===
                  session.id}
                aria-label={t('sessions.actions', 'Session actions')}
                aria-haspopup="menu"
                aria-expanded={openMenuSessionId === session.id}
                onclick={() => toggleMenu(session.id)}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <circle cx="3" cy="8" r="1.4" />
                  <circle cx="8" cy="8" r="1.4" />
                  <circle cx="13" cy="8" r="1.4" />
                </svg>
              </button>
              {#if openMenuSessionId === session.id}
                <div class="session-row__menu" role="menu">
                  <button
                    type="button"
                    class="session-row__menu-item"
                    role="menuitem"
                    onclick={() => startRename(session)}
                  >
                    {t('sessions.rename', 'Rename')}
                  </button>
                </div>
              {/if}
            </div>
          {/if}
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
    position: relative;
    flex: 0 0 auto;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-2);
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
    border-radius: inherit;
    padding: 12px 36px 11px 14px;
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

  .session-row__actions {
    position: absolute;
    top: 7px;
    right: 7px;
  }

  .session-row__menu-trigger {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    opacity: 0.5;
    cursor: pointer;
    transition:
      background 150ms ease,
      color 150ms ease,
      opacity 150ms ease;
  }

  .session-row:hover .session-row__menu-trigger,
  .session-row__menu-trigger:focus-visible,
  .session-row__menu-trigger--open {
    opacity: 1;
  }

  .session-row__menu-trigger:hover,
  .session-row__menu-trigger--open {
    background: var(--surface-3);
    color: var(--text-hi);
  }

  .session-row__menu-trigger:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 1px;
  }

  .session-row__menu-trigger svg {
    width: 15px;
    height: 15px;
    fill: currentColor;
  }

  .session-row__menu {
    position: absolute;
    top: calc(100% + 3px);
    right: 0;
    z-index: 20;
    min-width: 132px;
    padding: 4px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-3);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.32);
  }

  .session-row__menu-item {
    display: block;
    width: 100%;
    padding: 7px 9px;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 12.5px;
    text-align: left;
    cursor: pointer;
    transition: background 150ms ease;
  }

  .session-row__menu-item:hover,
  .session-row__menu-item:focus-visible {
    outline: none;
    background: rgba(232, 135, 10, 0.12);
  }

  .session-row__edit {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 11px 12px;
  }

  .session-row__edit-input {
    width: 100%;
    box-sizing: border-box;
    padding: 7px 9px;
    border: 1px solid var(--accent);
    border-radius: var(--r-sm);
    background: var(--surface);
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: 13px;
  }

  .session-row__edit-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(232, 135, 10, 0.25);
  }

  .session-row__edit-error {
    margin: 0;
    color: var(--red);
    font-size: 11.5px;
    line-height: 1.35;
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
