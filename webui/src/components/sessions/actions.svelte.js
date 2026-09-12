import { asText } from './presentation.js';
import {
  renameSession,
  setSessionCompactionPolicy,
  deleteSession,
} from '$lib/api.js';
import { t } from '$lib/i18n.js';
import { normalizeCompactionPolicy } from '$lib/compactionPolicy.js';
import { sessionDisplayName } from '$lib/sessionListView.js';

export function createSessionActions(context) {
  let editingSessionId = $state(null);

  let editingAgentAddress = $state('');

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

  // Enter inline-rename for a row. Seeds the field with the existing custom
  // title (empty when the row currently shows an automatic label, so the user
  // names it fresh).
  const startRename = (session) => {
    context.closeMenu();
    editingSessionId = session.id;
    editingAgentAddress = session.agent_address || asText(context.agentId);
    editValue = session.title ?? '';
    renameError = null;
  };

  const cancelRename = () => {
    editingSessionId = null;
    editingAgentAddress = '';
    editValue = '';
    renameError = null;
  };

  const submitRename = async () => {
    const sessionId = editingSessionId;
    const targetAgentId = editingAgentAddress || asText(context.agentId);
    if (!sessionId || !targetAgentId || renameSaving) {
      return;
    }

    renameSaving = true;
    renameError = null;
    try {
      await renameSession(targetAgentId, sessionId, editValue);
      editingSessionId = null;
      editingAgentAddress = '';
      editValue = '';
      // Re-fetch so the row reflects the server-normalized title (and the
      // fallback label when the name was cleared).
      await context.loadSessions(targetAgentId);
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
    context.closeMenu();
    const targetAgentId = asText(context.agentId);
    if (!targetAgentId || deleting) {
      return;
    }
    deleteConfirmSession = session;
  };

  const startPolicyEdit = (session) => {
    context.closeMenu();
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
    const targetAgentId =
      policySession?.agent_address || asText(context.agentId);
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
      await context.loadSessions(targetAgentId);
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
    const targetAgentId = session?.agent_address || asText(context.agentId);
    if (!session || !targetAgentId || deleting) {
      return;
    }

    deleting = true;
    actionError = null;
    try {
      const result = await deleteSession(targetAgentId, session.id);
      context.onSessionDeleted?.({
        deletedSessionId: session.id,
        nextSessionId: asText(result?.next_session_id),
        agentAddress: targetAgentId,
      });
      // Re-fetch so the deleted row disappears immediately, without waiting for
      // the resource_changed round-trip.
      await context.loadSessions(targetAgentId);
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
  return {
    get editingSessionId() {
      return editingSessionId;
    },
    set editingSessionId(value) {
      editingSessionId = value;
    },
    get editingAgentAddress() {
      return editingAgentAddress;
    },
    set editingAgentAddress(value) {
      editingAgentAddress = value;
    },
    get editValue() {
      return editValue;
    },
    set editValue(value) {
      editValue = value;
    },
    get renameError() {
      return renameError;
    },
    set renameError(value) {
      renameError = value;
    },
    get renameSaving() {
      return renameSaving;
    },
    set renameSaving(value) {
      renameSaving = value;
    },
    get actionError() {
      return actionError;
    },
    set actionError(value) {
      actionError = value;
    },
    get deleteConfirmSession() {
      return deleteConfirmSession;
    },
    set deleteConfirmSession(value) {
      deleteConfirmSession = value;
    },
    get policySession() {
      return policySession;
    },
    set policySession(value) {
      policySession = value;
    },
    get policyUsesOverride() {
      return policyUsesOverride;
    },
    set policyUsesOverride(value) {
      policyUsesOverride = value;
    },
    get policyDraft() {
      return policyDraft;
    },
    set policyDraft(value) {
      policyDraft = value;
    },
    get policySaving() {
      return policySaving;
    },
    set policySaving(value) {
      policySaving = value;
    },
    get policyError() {
      return policyError;
    },
    set policyError(value) {
      policyError = value;
    },
    get SESSION_TITLE_MAX_LENGTH() {
      return SESSION_TITLE_MAX_LENGTH;
    },
    get startRename() {
      return startRename;
    },
    get requestDelete() {
      return requestDelete;
    },
    get startPolicyEdit() {
      return startPolicyEdit;
    },
    get closePolicyEdit() {
      return closePolicyEdit;
    },
    get savePolicy() {
      return savePolicy;
    },
    get deleteConfirmMessage() {
      return deleteConfirmMessage;
    },
    set deleteConfirmMessage(value) {
      deleteConfirmMessage = value;
    },
    get cancelDelete() {
      return cancelDelete;
    },
    get confirmDelete() {
      return confirmDelete;
    },
    get handleRenameKeydown() {
      return handleRenameKeydown;
    },
  };
}
