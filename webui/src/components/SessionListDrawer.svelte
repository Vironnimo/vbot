<script>
  import Badge from './ui/Badge.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ConfirmDialog from './ui/ConfirmDialog.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import Modal from './ui/Modal.svelte';
  import Toggle from './ui/Toggle.svelte';
  import CompactionPolicyEditor from './compaction/CompactionPolicyEditor.svelte';
  import {
    deleteSession,
    listSessions,
    renameSession,
    setSessionCompactionPolicy,
  } from '$lib/api.js';
  import { normalizeCompactionPolicy } from '$lib/compactionPolicy.js';
  import { activeLocaleTag, t } from '$lib/i18n.js';
  import { tooltip } from '$lib/tooltip.js';
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
    // switched session created in another window appears here automatically.
    reloadToken = 0,
    onSessionSelected = () => {},
    // Called after a successful delete with { deletedSessionId, nextSessionId }
    // so ChatView can navigate if it was viewing the removed session (#2).
    onSessionDeleted = () => {},
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
  // Row-delete state: a transient error surfaced when a delete is refused (for
  // example a busy session, #4) and an in-flight guard against double-clicks.
  let actionError = $state(null);
  let deleting = $state(false);
  // The session awaiting delete confirmation (null = dialog closed). The delete
  // only runs once the confirm dialog resolves.
  let deleteConfirmSession = $state(null);
  let policySession = $state(null);
  let policyUsesOverride = $state(false);
  let policyDraft = $state(null);
  let policySaving = $state(false);
  let policyError = $state(null);

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

  // Delete (archive) a session from the row menu. The shared ConfirmDialog
  // guards the click (#3); for a channel-bound session the body also notes it
  // will resume empty on the next inbound message (#5a). The server returns
  // where to land, which ChatView uses to navigate if it was viewing the
  // removed session.
  const requestDelete = (session) => {
    closeMenu();
    const targetAgentId = asText(agentId);
    if (!targetAgentId || deleting) {
      return;
    }
    deleteConfirmSession = session;
  };

  const startPolicyEdit = (session) => {
    closeMenu();
    policySession = session;
    policyUsesOverride = Boolean(session.compaction_policy_override);
    policyDraft = normalizeCompactionPolicy(
      session.compaction_policy_override ?? session.compaction_policy_effective,
    );
    policyError = null;
  };

  const closePolicyEdit = () => {
    if (policySaving) return;
    policySession = null;
    policyDraft = null;
    policyError = null;
  };

  const savePolicy = async () => {
    const targetAgentId = asText(agentId);
    if (!targetAgentId || !policySession || policySaving) return;
    policySaving = true;
    policyError = null;
    try {
      await setSessionCompactionPolicy(
        targetAgentId,
        policySession.id,
        policyUsesOverride ? normalizeCompactionPolicy(policyDraft) : null,
      );
      policySession = null;
      policyDraft = null;
      await loadSessions(targetAgentId);
    } catch (error) {
      policyError =
        error.message ||
        t(
          'sessions.compactionSaveError',
          'The Compaction Policy could not be saved.',
        );
    } finally {
      policySaving = false;
    }
  };

  // The confirm body reflects whether the pending session is channel-bound.
  let deleteConfirmMessage = $derived.by(() => {
    const session = deleteConfirmSession;
    if (!session) {
      return '';
    }
    const name = session.display_name || sessionDisplayName(session);
    return session.is_channel_session
      ? t(
          'sessions.delete_confirm_channel',
          'Delete session "{name}"? It is archived and can be restored. The channel ' +
            'conversation will start fresh on the next incoming message.',
          { name },
        )
      : t(
          'sessions.delete_confirm',
          'Delete session "{name}"? It is archived and can be restored.',
          { name },
        );
  });

  const cancelDelete = () => {
    deleteConfirmSession = null;
  };

  const confirmDelete = async () => {
    const session = deleteConfirmSession;
    deleteConfirmSession = null;
    const targetAgentId = asText(agentId);
    if (!session || !targetAgentId || deleting) {
      return;
    }

    deleting = true;
    actionError = null;
    try {
      const result = await deleteSession(targetAgentId, session.id);
      onSessionDeleted?.({
        deletedSessionId: session.id,
        nextSessionId: asText(result?.next_session_id),
      });
      // Re-fetch so the deleted row disappears immediately, without waiting for
      // the resource_changed round-trip.
      await loadSessions(targetAgentId);
    } catch (error) {
      actionError =
        error.message ||
        t('sessions.delete_error', 'The session could not be deleted.');
    } finally {
      deleting = false;
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
  </div>

  {#if actionError}
    <p class="session-drawer__state session-drawer__state--error" role="alert">
      {actionError}
    </p>
  {/if}

  {#if sessionState.error}
    <Banner variant="error" class="session-drawer__load-error" role="alert">
      <span>{sessionState.error}</span>
      <Button
        variant="secondary"
        disabled={sessionState.loading || !agentId}
        onClick={() => loadSessions()}
      >
        {t('common.retry', 'Retry')}
      </Button>
    </Banner>
  {:else if sessionState.loading && sessionState.sessions.length === 0}
    <p class="session-drawer__state">
      {t('sessions.loading', 'Loading sessions…')}
    </p>
  {:else if sessionState.sessions.length === 0}
    <EmptyState
      density="compact"
      class="session-drawer__empty-layout"
      title={t('chat.sessions.emptyTitle', 'No sessions yet')}
      description={t(
        'sessions.no_sessions',
        'No sessions found for this agent.',
      )}
    />
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
                <p
                  class="session-row__name"
                  use:tooltip={session.display_name ||
                    sessionDisplayName(session)}
                >
                  {session.display_name || sessionDisplayName(session)}
                </p>
                {#if session.has_unread_completion}
                  <span
                    class="session-row__unread"
                    use:tooltip={t(
                      'sessions.unreadCompletionHint',
                      'This Session has an unread result.',
                    )}
                  >
                    <span class="session-row__unread-dot" aria-hidden="true"
                    ></span>
                    <span>{t('sessions.unreadCompletion', 'Unread')}</span>
                  </span>
                {/if}
                {#if session.id === asText(agentCurrentSessionId)}
                  <Badge variant="success">
                    {t('sessions.current', 'Current')}
                  </Badge>
                {/if}
                {#if session.platform}
                  <Badge variant="info">
                    {#if session.platform === 'telegram'}
                      <svg
                        viewBox="0 0 18 18"
                        width="10"
                        height="10"
                        fill="currentColor"
                        aria-hidden="true"
                      >
                        <path
                          d="M15.36 3.27c.39-.15.77.2.67.61l-1.94 9.14c-.07.34-.45.5-.74.31l-3.16-2.13-1.62 1.57c-.22.22-.6.11-.67-.2l-.52-2.41 6.72-5.91c.14-.12-.04-.35-.2-.24L5.6 9.04 2.5 7.8c-.34-.13-.35-.6-.02-.75l12.88-3.78z"
                        />
                      </svg>
                    {/if}
                    <span>{resolvePlatformLabel(session.platform)}</span>
                  </Badge>
                {/if}
                {#if session.is_subagent_session}
                  <span
                    class="tooltip-anchor"
                    use:tooltip={t(
                      'sessions.subagentHint',
                      'A session run by a sub-agent working on behalf of a parent session. The parent is shown below.',
                    )}
                  >
                    <Badge variant="neutral">
                      {t('chat.subagent.label', 'Sub-agent')}
                    </Badge>
                  </span>
                {/if}
                {#if session.is_fork}
                  <span
                    class="tooltip-anchor"
                    use:tooltip={t(
                      'sessions.forkHint',
                      'A copy of another session. Background reflection and /reflect review a conversation in a fork so the original session stays untouched.',
                    )}
                  >
                    <Badge variant="neutral">
                      {t('sessions.fork', 'Fork')}
                    </Badge>
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
                  <button
                    type="button"
                    class="session-row__menu-item"
                    role="menuitem"
                    onclick={() => startPolicyEdit(session)}
                  >
                    {t('sessions.compactionPolicy', 'Compaction Policy')}
                  </button>
                  <button
                    type="button"
                    class="session-row__menu-item session-row__menu-item--danger"
                    role="menuitem"
                    onclick={() => requestDelete(session)}
                  >
                    {t('sessions.delete', 'Delete')}
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

{#if deleteConfirmSession}
  <ConfirmDialog
    title={t('sessions.delete_confirm_title', 'Delete session')}
    body={deleteConfirmMessage}
    confirmLabel={t('common.delete', 'Delete')}
    onConfirm={confirmDelete}
    onCancel={cancelDelete}
  />
{/if}

{#if policySession}
  <Modal
    title={t('sessions.compactionPolicy', 'Compaction Policy')}
    labelledById="session-compaction-policy-title"
    closeDisabled={policySaving}
    onClose={closePolicyEdit}
  >
    {#snippet body()}
      <div class="modal-body session-policy-modal__body">
        <div class="session-policy-modal__inheritance">
          <div>
            <div class="session-policy-modal__label">
              {t('sessions.compactionOverride', 'Session override')}
            </div>
            <p>
              {t(
                'sessions.compactionOverrideDescription',
                'When disabled, this Session follows later Agent or global Policy changes.',
              )}
            </p>
          </div>
          <Toggle
            checked={policyUsesOverride}
            disabled={policySaving}
            ariaLabel={t('sessions.compactionOverride', 'Session override')}
            onChange={(enabled) => (policyUsesOverride = enabled)}
          />
        </div>
        <CompactionPolicyEditor
          value={policyDraft}
          disabled={!policyUsesOverride || policySaving}
          idPrefix="session-compaction-policy"
          onChange={(next) => (policyDraft = next)}
        />
        {#if policyError}
          <p class="session-policy-modal__error" role="alert">{policyError}</p>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={policySaving}
        onClick={closePolicyEdit}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button disabled={policySaving} onClick={savePolicy}>
        {policySaving
          ? t('common.saving', 'Saving…')
          : t('common.save', 'Save')}
      </Button>
    {/snippet}
  </Modal>
{/if}

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

  :global(.session-policy-modal__body) {
    display: grid;
    gap: 18px;
    min-width: min(620px, 78vw);
  }

  .session-policy-modal__inheritance {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
  }

  .session-policy-modal__label {
    color: var(--text-hi);
    font: 500 13px var(--font-ui);
  }

  .session-policy-modal__inheritance p,
  .session-policy-modal__error {
    margin: 3px 0 0;
    color: var(--text-med);
    font: 12px/1.45 var(--font-ui);
  }

  .session-policy-modal__error {
    color: var(--danger);
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
    font-size: var(--fs-mono-xs);
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
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
    border-color: var(--accent-40);
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
    background: var(--accent-08);
  }

  .session-row__select--active {
    background: var(--accent-12);
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
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
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
    font-size: var(--fs-body-sm);
    text-align: left;
    cursor: pointer;
    transition: background 150ms ease;
  }

  .session-row__menu-item:hover,
  .session-row__menu-item:focus-visible {
    outline: none;
    background: var(--accent-12);
  }

  .session-row__menu-item--danger {
    color: var(--red);
  }

  .session-row__menu-item--danger:hover,
  .session-row__menu-item--danger:focus-visible {
    background: rgba(252, 129, 129, 0.14);
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
    font-size: var(--fs-label-md);
  }

  .session-row__edit-input:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: var(--focus-ring);
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
    font-size: var(--fs-label-md);
    font-weight: 600;
    line-height: 1.25;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-row__unread {
    display: inline-flex;
    flex-shrink: 0;
    align-items: center;
    gap: 5px;
    color: var(--blue);
    font-size: var(--fs-label-sm);
    font-weight: 600;
  }

  .session-row__unread-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--blue);
    box-shadow: 0 0 0 2px var(--blue-dim);
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
    font-size: var(--fs-mono-sm);
  }

  .session-drawer__state {
    margin: 0;
    padding: 10px 12px;
    color: var(--text-med);
    font-size: var(--fs-label-sm);
  }

  .session-drawer__state--error {
    color: var(--red);
  }

  :global(.session-drawer__load-error) {
    margin: 10px 12px;
  }

  :global(.session-drawer__empty-layout) {
    margin: 12px;
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
