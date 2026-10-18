import { t } from '../i18n.js';
import { formatAgentAddress, parseAgentAddress } from '../agentAddress.js';
import {
  sessionKey,
  supersedeRenamedSessionKey,
  syncQueueFromServer,
  removeQueuedMessage,
} from './sessionState.js';

const RPC_ERROR_QUEUE_ITEM_STEERING = 'queue_item_steering';

// Authoritative Queue reads and mutations share one freshness boundary.
export function createChatQueue({
  chatState,
  operations,
  isDisplayedSession,
  errorMessage,
  onRestartQueueDiscarded,
}) {
  const queueSyncVersions = new Map();
  let handledQueueInvalidation = null;

  function invalidateQueueSync(sessionState) {
    const version = (queueSyncVersions.get(sessionState.key) ?? 0) + 1;
    queueSyncVersions.set(sessionState.key, version);
    return version;
  }

  // Reads a Session's Queue again after its Run ended or another window
  // changed it, where the Queue is shown or still holds items. Any other
  // Session reads its Queue when opened.
  async function refreshSessionQueue(sessionState) {
    if (
      isDisplayedSession(sessionState?.agentId, sessionState?.sessionId) ||
      sessionState?.queue?.length > 0
    ) {
      await syncSessionQueue(sessionState);
    }
  }

  async function syncSessionQueue(sessionState) {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      return;
    }
    const requestVersion = invalidateQueueSync(sessionState);
    const isLatestRequest = () =>
      queueSyncVersions.get(sessionState.key) === requestVersion;
    try {
      const result = await operations.listQueue(
        sessionState.agentId,
        sessionState.sessionId,
      );
      if (!isLatestRequest()) {
        return;
      }
      syncQueueFromServer(sessionState, result?.items ?? []);
    } catch (error) {
      if (!isLatestRequest()) {
        return;
      }
      sessionState.actionError = `${t('queue.syncError')} ${errorMessage(error)}`;
    }
  }

  async function steerQueued(sessionState, itemId) {
    if (!sessionState) return false;
    sessionState.actionError = '';
    try {
      await operations.steerQueueItem(
        sessionState.agentId,
        sessionState.sessionId,
        itemId,
      );
      await syncSessionQueue(sessionState);
      return true;
    } catch (error) {
      sessionState.actionError = `${t('queue.steerError')} ${errorMessage(error)}`;
      await syncSessionQueue(sessionState);
      return false;
    }
  }

  async function removeQueued(sessionState, queuedMessageId) {
    if (!sessionState) {
      return;
    }
    sessionState.actionError = '';
    try {
      await operations.removeFromQueue(
        sessionState.agentId,
        sessionState.sessionId,
        queuedMessageId,
      );
      invalidateQueueSync(sessionState);
      removeQueuedMessage(sessionState, queuedMessageId);
    } catch (error) {
      if (await reportQueueItemSteering(sessionState, error)) return;
      sessionState.actionError = `${t('queue.removeError')} ${errorMessage(error)}`;
    }
  }

  // The item is already bound for the running Run; refresh the Queue so its
  // steering state (or its delivery) replaces the stale local controls.
  async function reportQueueItemSteering(sessionState, error) {
    if (error?.code !== RPC_ERROR_QUEUE_ITEM_STEERING) return false;
    sessionState.actionError = t('queue.steeringLocked');
    await syncSessionQueue(sessionState);
    return true;
  }

  async function updateQueued(
    sessionState,
    queuedMessageId,
    newContent,
    fileMentions,
  ) {
    if (!sessionState) {
      return false;
    }
    sessionState.actionError = '';
    try {
      const normalizedFileMentions = Array.isArray(fileMentions)
        ? fileMentions
        : [];
      await operations.updateQueueItem(
        sessionState.agentId,
        sessionState.sessionId,
        queuedMessageId,
        newContent,
        { fileMentions: normalizedFileMentions },
      );
      // Acceptance proves this edit was saved, not that it is still current:
      // another window may have changed or consumed the item before this reply.
      // The fresh read also retires any Queue read that predates acceptance.
      await syncSessionQueue(sessionState);
      return true;
    } catch (error) {
      if (await reportQueueItemSteering(sessionState, error)) return false;
      sessionState.actionError = `${t('queue.editError')} ${errorMessage(error)}`;
      return false;
    }
  }

  function applyConnectionSnapshot(snapshot) {
    if (Array.isArray(snapshot.queues)) {
      const queueItemsBySession = new Map();
      for (const queueScope of snapshot.queues) {
        if (
          typeof queueScope?.agent_id !== 'string' ||
          typeof queueScope?.session_id !== 'string'
        ) {
          continue;
        }
        queueItemsBySession.set(
          sessionKey(
            formatAgentAddress(queueScope.agent_id, queueScope.project_id),
            queueScope.session_id,
          ),
          Array.isArray(queueScope.items) ? queueScope.items : [],
        );
      }

      let discardedCount = 0;
      for (const sessionState of Object.values(chatState.sessions)) {
        const serverItems = queueItemsBySession.get(sessionState.key) ?? [];
        if (snapshot.replay_status === 'epoch_changed') {
          const serverItemIds = new Set(
            serverItems.map((item) => item?.id).filter(Boolean),
          );
          discardedCount += sessionState.queue.filter(
            (item) => item?.id && !serverItemIds.has(item.id),
          ).length;
        }
        invalidateQueueSync(sessionState);
        syncQueueFromServer(sessionState, serverItems);
      }
      if (discardedCount > 0) {
        onRestartQueueDiscarded(discardedCount);
      }
    }
  }

  function applyQueueInvalidation(scope) {
    if (!scope || scope === handledQueueInvalidation) {
      return false;
    }
    handledQueueInvalidation = scope;
    if (!scope.sessionId) {
      return true;
    }
    for (const sessionState of Object.values(chatState.sessions)) {
      const { agentId } = parseAgentAddress(sessionState.agentId);
      if (
        sessionState.sessionId === scope.sessionId &&
        agentId === scope.agentId
      ) {
        void refreshSessionQueue(sessionState);
      }
    }
    return true;
  }

  return {
    refreshSessionQueue,
    syncSessionQueue,
    steerQueued,
    removeQueued,
    updateQueued,
    applyQueueInvalidation,
    applyConnectionSnapshot,
    renameSession(oldKey, newKey) {
      supersedeRenamedSessionKey(queueSyncVersions, oldKey, newKey);
    },
    destroy() {
      queueSyncVersions.clear();
    },
  };
}
