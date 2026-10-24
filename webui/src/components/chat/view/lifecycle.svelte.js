import { SvelteSet } from 'svelte/reactivity';
import {
  createChatController,
  selectAgent,
  setAgents,
} from '$lib/chatState.js';
import { createChatRunStream } from '$lib/chatRunStream.js';
import { subscribeRunEvents } from '$lib/api.js';
import { t } from '$lib/i18n.js';
import { onMount, untrack } from 'svelte';

// Own this retained Chat area's roster, event inputs, stream and browser lifetime.
export function createChatViewLifecycle(context) {
  const {
    chatState,
    target,
    navigation,
    actions,
    sessionSettings,
    reportAgentSelected,
  } = context;
  let lastCommandsAddress = undefined;
  let lastSharedSelectedAgentId = '';
  let lastSharedAgents = null;
  let lastAgentsRefreshToken = null;

  $effect(() => {
    if (
      context.sharedAgents.length > 0 &&
      context.sharedAgents !== lastSharedAgents
    ) {
      lastSharedAgents = context.sharedAgents;
      setAgents(chatState, context.sharedAgents, {
        preserveSessionSelection: context.preserveSessionSelection,
      });
    }
  });

  let initialSharedAgentSyncDone = false;
  $effect(() => {
    if (!context.active) {
      return;
    }
    const firstSync = !initialSharedAgentSyncDone;
    initialSharedAgentSyncDone = true;
    if (
      context.sharedSelectedAgentId &&
      context.sharedSelectedAgentId !== chatState.selectedAgentId &&
      chatState.agents.some(
        (agent) => agent.id === context.sharedSelectedAgentId,
      )
    ) {
      lastSharedSelectedAgentId = context.sharedSelectedAgentId;
      if (firstSync) {
        // Mount-time prop reconciliation, not user navigation: sync silently.
        // Routing this through handleSelectAgent would clear a just-restored
        // override and report a session navigation during a history restore —
        // the report then pushes a phantom entry over the restored one (the
        // mount-path echo hole). The controller still loads the adopted
        // Agent's current History when Chat was mounted hidden.
        selectAgent(chatState, context.sharedSelectedAgentId);
        void chatController.loadAdoptedSelectionHistory();
        return;
      }
      // Following the shared selection corrects Chat's place; the choice was
      // made (and recorded) where the Agent was selected.
      navigation.handleSelectAgent(context.sharedSelectedAgentId, {
        focusComposer: false,
        step: false,
      });
    }
  });

  // An Identity Agent was renamed (from any window or the CLI): this area
  // keeps showing the same Session, with its History, drafts and Session
  // settings, under the new id. A History read in flight for the old id is
  // superseded, so the shown Session is read again.
  function renameAgent(oldAgentId, newAgentId) {
    if (!oldAgentId || !newAgentId || oldAgentId === newAgentId) {
      return;
    }
    chatController.renameAgent(oldAgentId, newAgentId);
    navigation.renameAgent(oldAgentId, newAgentId);
    sessionSettings.renameAgent(oldAgentId, newAgentId);
    actions.renameAgent(oldAgentId, newAgentId);
    if (lastSharedSelectedAgentId === oldAgentId) {
      lastSharedSelectedAgentId = newAgentId;
    }
    const shown = target.activeSessionState;
    if (shown?.agentId === newAgentId) {
      void actions.loadHistoryForSession(newAgentId, shown.sessionId);
    }
  }

  $effect(() => context.subscribeAgentRenames?.(renameAgent));

  // The controller owns reconnect deduplication and reconciliation; the View
  // only forwards the latest reactive input.
  $effect(() => {
    chatController.applyConnectionSnapshot(context.connectionSnapshot);
  });

  $effect(() => {
    if (lastAgentsRefreshToken === null) {
      lastAgentsRefreshToken = context.agentsRefreshToken;
      return;
    }
    if (context.agentsRefreshToken !== lastAgentsRefreshToken) {
      lastAgentsRefreshToken = context.agentsRefreshToken;
      loadAgents({
        preferredAgentId: context.sharedSelectedAgentId,
        silent: true,
      });
    }
  });

  // A shown Chat applies each Run event as it arrives. A hidden one wakes only
  // for Run starts; the controller applies the rest when it is shown again.
  $effect(() => {
    if (!context.runEvents) {
      return;
    }
    const deferred = !context.active;
    void (deferred ? context.runEvents.runStarts : context.runEvents.end);
    untrack(() => chatController.handleRunEvents({ deferred }));
  });

  $effect(() => {
    const statuses = context.commandStatuses;
    untrack(() => chatController.applyCommandStatuses(statuses));
  });

  // App's Session activity decides Agent activity and which unread results
  // count (only those of idle Sessions).
  $effect(() => {
    chatState.sessionActivity = context.sessionActivity;
  });

  $effect(() => {
    const sessionState = context.active ? target.activeSessionState : null;
    void context.runEvents?.lifecycle;
    void context.commandStatuses;
    void context.queueInvalidation;
    void context.terminalsRefreshToken;
    void context.connectionSnapshot;
    void context.sessionsRefreshToken;
    void context.sessionInvalidations;
    untrack(() => chatController.syncSessionWork(sessionState));
  });

  // Durable completion activity for the displayed Agent addresses. A reconnect
  // or a full Session refresh re-reads every address; an address joining the
  // set is read once. Scoped Session invalidations are applied separately.
  $effect(() => {
    const addresses = context.activity.pickerAgentAddresses;
    const reconnectRevision = context.connectionSnapshot
      ? `${context.connectionSnapshot.epoch ?? ''}:${context.connectionSnapshot.last_sequence ?? ''}`
      : '';
    const refreshKey = `${context.sessionsRefreshToken}:${reconnectRevision}`;
    untrack(() => chatController.syncAgentActivity(addresses, refreshKey));
  });

  $effect(() => {
    const entries = context.sessionInvalidations;
    untrack(() => chatController.applySessionInvalidations(entries));
  });

  // A read acknowledgement tells every connected app, including the vBot tray,
  // that the user has seen the result, so only a page the user is looking at
  // (visible and focused) sends it. Attention is read live; regaining focus or
  // visibility bumps the revision so the displayed Session is checked again.
  let pageAttentionRevision = $state(0);
  const pageAttended = () =>
    document.visibilityState === 'visible' && document.hasFocus();

  $effect(() => {
    void pageAttentionRevision;
    const sessionState = target.activeSessionState;
    const known = sessionState?.readRunIds ?? [];
    const visible = sessionState?.focusedHistory ?? sessionState;
    const runIds = [
      ...new SvelteSet([
        ...Object.entries(visible?.historyRuns ?? {})
          .filter(([, run]) => run.complete)
          .map(([id]) => id),
        ...(visible?.runEvents ?? [])
          .filter((event) =>
            [
              'run_completed',
              'run_failed',
              'run_interrupted',
              'run_cancelled',
            ].includes(event.type),
          )
          .map((event) => event.run_id),
      ]),
    ]
      .filter((id) => id && !known.includes(id))
      .slice(0, 200);
    const unreadRunId = runIds.join(',');
    if (
      !context.active ||
      !unreadRunId ||
      sessionState.markReadFailedRunId === unreadRunId ||
      !target.isDisplayedSession(
        sessionState.agentId,
        sessionState.sessionId,
      ) ||
      sessionState?.localUnreadCount === 0 ||
      !pageAttended()
    ) {
      return;
    }
    void chatController.markSessionCompletionRead(sessionState, runIds);
  });

  // Re-sync a held session's queue when another window mutates it. App forwards
  // the generic `resource_changed(kind:"queue")` signal as a scope object (a
  // fresh object per signal, so this re-fires even for a repeat scope). Only
  // sessions we actually hold are synced — the queue RPC keys on the bare agent
  // id, so match the scope's bare agent id + session id against each held
  // session — and of those only where this Chat shows the Queue or still holds
  // items; any other reads its Queue when opened. The viewed conversation is
  // never switched.
  $effect(() => {
    chatController.applyQueueInvalidation(context.queueInvalidation);
  });

  onMount(() => {
    loadAgents({ preferredAgentId: context.sharedSelectedAgentId });
    const onPageAttentionChange = () => {
      pageAttentionRevision += 1;
    };
    const onVisibilityChange = () => {
      onPageAttentionChange();
      if (document.visibilityState !== 'visible') {
        return;
      }
      const sessionState = target.activeSessionState;
      if (
        !sessionState?.currentRun?.runId ||
        sessionState.status !== 'running'
      ) {
        return;
      }
      if (sessionState.streamError) {
        void chatController.reconcileRunSession(
          sessionState,
          sessionState.currentRun.runId,
        );
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('focus', onPageAttentionChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('focus', onPageAttentionChange);
      chatController.destroy();
    };
  });

  // Reload command/skill suggestions whenever the context.active address, the
  // Project its Skills come from (the Session's, or the one a draft chose) or
  // the live command catalog changes. The token does not disturb the draft or
  // context.active selection. The mount's roster read decides the first address, so
  // nothing loads before it ends.
  $effect(() => {
    if (chatState.loadingAgents) {
      return;
    }
    const { agentAddress } = target.activeAddressing();
    const scope = sessionSettings.readScope();
    const commandsKey = `${context.commandsRefreshToken}:${agentAddress}:${JSON.stringify(scope)}`;
    if (commandsKey === lastCommandsAddress) {
      return;
    }
    lastCommandsAddress = commandsKey;
    chatController.loadCommands(agentAddress, scope);
  });

  const loadAgents = (options = {}) => chatController.loadAgents(options);

  const runStream = createChatRunStream({
    childLimit: 100,
    chatState,
    subscribeRunEvents,
    refreshSessionQueue: (sessionState) =>
      chatController.refreshSessionQueue(sessionState),
    reconcileRunSession: (sessionState, expectedRunId) =>
      chatController.reconcileRunSession(sessionState, expectedRunId),
    isDisplayedSession: target.isDisplayedSession,
    updateSubAgentRunStatuses: (updates, options) =>
      chatController.applySubAgentStatusUpdates(updates, options),
    // A finished review's row reports what it changed, displayed or not.
    onReflectionFinished: (sourceState) =>
      chatController.refreshReflections(sourceState),
  });
  const chatController = createChatController({
    submissionScope: untrack(() => context.draftScope ?? 0),
    chatState,
    preserveSessionSelection: untrack(() => context.preserveSessionSelection),
    runStream,
    runEvents: untrack(() => context.runEvents),
    isDisplayedSession: target.isDisplayedSession,
    shouldLoadCurrentHistory: () =>
      !navigation.viewingSessionId && !target.projectAgentActive,
    onAgentsChanged: (agents) => context.onAgentsChanged?.(agents),
    onAgentSelected: reportAgentSelected,
    onSessionCreated: (sessionState) => {
      navigation.adoptCreatedSession(
        sessionState.agentId,
        sessionState.sessionId,
      );
      context.onSessionCreated();
    },
    onRestartQueueDiscarded: (count) => {
      actions.showChatToast(
        count === 1
          ? t('queue.restartDiscardedOne')
          : t('queue.restartDiscardedMany', { count }),
      );
    },
  });
  // Before the snapshot/event effects run: a fresh owner adopts the current
  // Run state instead of replaying the events already in App's feed.
  untrack(() =>
    chatController.startFromServerState(context.connectionSnapshot),
  );

  return {
    controller: chatController,
    get lastSharedSelectedAgentId() {
      return lastSharedSelectedAgentId;
    },
    set lastSharedSelectedAgentId(value) {
      lastSharedSelectedAgentId = value;
    },
  };
}
