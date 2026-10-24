import {
  cancelRun as requestCancelRun,
  cancelToolCall as requestCancelToolCall,
  controlRun as requestControlRun,
  editChatMessage as requestEditChatMessage,
  getSession as requestGetSession,
  getSessionWork as requestGetSessionWork,
  getSessionChangeStats as requestGetSessionChangeStats,
  getStatisticsSessionUsage as requestGetSessionUsage,
  getSubAgentTree as requestGetSubAgentTree,
  inspectSubAgentWork as requestInspectSubAgentWork,
  listAgents as requestListAgents,
  listChatCommands as requestListChatCommands,
  listFiles as requestListFiles,
  listQueue as requestListQueue,
  listSessionActivity as requestListSessionActivity,
  listSessions as requestListSessions,
  loadChatTimeline as requestLoadChatHistory,
  loadChatTimelineChildren as requestLoadTimelineChildren,
  loadChatToolCall as requestLoadToolCall,
  loadCommandOutput as requestLoadCommandOutput,
  loadLearningChanges as requestLoadLearningChanges,
  loadReflectionRuns as requestLoadReflectionRuns,
  markSessionRead as requestMarkSessionRead,
  removeFromQueue as requestRemoveFromQueue,
  setSessionAgentOverrides as requestSetSessionAgentOverrides,
  showProject as requestShowProject,
  startChatRun as requestStartChatRun,
  updateQueueItem as requestUpdateQueueItem,
  steerQueueItem as requestSteerQueueItem,
  stopAll as requestStopAll,
  stopCommand as requestStopCommand,
  undoLearningChanges as requestUndoLearningChanges,
} from '../api.js';
import { t } from '../i18n.js';
import { markWidgetStatesSent } from '../widgetStates.svelte.js';
import { createChatHistory } from './historyLoading.js';
import { createChatQueue } from './queue.js';
import { createChatActivity } from './activity.js';
import { createChatChildTasks } from './childTasks.js';
import { createChatReflections } from './reflections.js';
import { createChatWork } from './work.js';
import { formatAgentAddress, parseAgentAddress } from '../agentAddress.js';
import {
  setAgents,
  selectAgent,
  selectedAgent,
  sessionKey,
  ensureSessionState,
  renameAgentInState,
  isRecord,
} from './sessionState.js';
import { truncateSessionForEdit } from './history.js';
import { startRun } from './runEvents.js';
import { takeSessionInvalidations } from '../sessionInvalidation.js';
import { resolveMoveActionFromResponse } from './addressing.js';
import { createChatSubmissions } from './submissions.js';

function defaultChatOperations() {
  return {
    cancelRun: (...args) => requestCancelRun(...args),
    cancelToolCall: (...args) => requestCancelToolCall(...args),
    controlRun: (...args) => requestControlRun(...args),
    editChatMessage: (...args) => requestEditChatMessage(...args),
    inspectSubAgentWork: (...args) => requestInspectSubAgentWork(...args),
    listAgents: (...args) => requestListAgents(...args),
    listChatCommands: (...args) => requestListChatCommands(...args),
    listFiles: (...args) => requestListFiles(...args),
    listQueue: (...args) => requestListQueue(...args),
    listSessionActivity: (...args) => requestListSessionActivity(...args),
    listSessions: (...args) => requestListSessions(...args),
    getSession: (...args) => requestGetSession(...args),
    getSessionWork: (...args) => requestGetSessionWork(...args),
    getSessionChangeStats: (...args) => requestGetSessionChangeStats(...args),
    getSessionUsage: (...args) => requestGetSessionUsage(...args),
    getSubAgentTree: (...args) => requestGetSubAgentTree(...args),
    loadChatHistory: (...args) => requestLoadChatHistory(...args),
    loadTimelineChildren: (...args) => requestLoadTimelineChildren(...args),
    loadToolCall: (...args) => requestLoadToolCall(...args),
    loadCommandOutput: (...args) => requestLoadCommandOutput(...args),
    loadLearningChanges: (...args) => requestLoadLearningChanges(...args),
    loadReflectionRuns: (...args) => requestLoadReflectionRuns(...args),
    markSessionRead: (...args) => requestMarkSessionRead(...args),
    removeFromQueue: (...args) => requestRemoveFromQueue(...args),
    setSessionAgentOverrides: (...args) =>
      requestSetSessionAgentOverrides(...args),
    showProject: (...args) => requestShowProject(...args),
    startChatRun: (...args) => requestStartChatRun(...args),
    updateQueueItem: (...args) => requestUpdateQueueItem(...args),
    steerQueueItem: (...args) => requestSteerQueueItem(...args),
    stopAll: (...args) => requestStopAll(...args),
    stopCommand: (...args) => requestStopCommand(...args),
    undoLearningChanges: (...args) => requestUndoLearningChanges(...args),
  };
}

// Own Chat's asynchronous lifecycle end-to-end. ChatView supplies only the few
// navigation/display facts the controller cannot derive from Chat state; RPC
// sequencing, durable-history reconciliation, Queue updates, run transitions,
// error state, reconnects, and subscription cleanup stay behind this boundary.
export function createChatController({
  chatState,
  runStream,
  operations = defaultChatOperations(),
  // The App's Run event feed (`runEventFeed.svelte.js`); without one this
  // owner applies no Run server events.
  runEvents = null,
  isDisplayedSession = () => false,
  shouldLoadCurrentHistory = () => true,
  onAgentsChanged = () => {},
  preserveSessionSelection = false,
  onAgentSelected = () => {},
  onSessionCreated = () => {},
  onRestartQueueDiscarded = () => {},
  submissionScope = '',
}) {
  let handledConnectionSnapshot = null;
  let commandsLoadVersion = 0;
  let agentsLoadVersion = 0;
  let initialHistoryPending = false;
  const submitChat = createChatSubmissions((params) =>
    operations.startChatRun(params),
  );

  // Feed positions: the newest one this owner applied, and the newest one a
  // hidden owner scanned for its displayed Session's Run starts. Those starts
  // apply early, at the positions in `earlyRunStarts`.
  let runEventCursor = 0;
  let runStartCursor = 0;
  const earlyRunStarts = new Set();

  const work = createChatWork({ operations });
  const childTasks = createChatChildTasks({
    chatState,
    operations,
    errorMessage,
  });
  const {
    applyCommandStatuses,
    applySubAgentStatusUpdates,
    cancelSubAgent,
    loadCommandOutput,
    loadSubAgentWork,
    reconcileSubAgentRows,
    stopCommand,
    stopSubAgentWork,
  } = childTasks;
  const activity = createChatActivity({
    chatState,
    operations,
    errorMessage,
    retainsTerminalRunEvent: (runId) =>
      runEvents?.retainsTerminal(runId) === true,
    applyDeferredRunEvents: () => applyRunEvents(),
  });
  const reflections = createChatReflections({
    operations,
    isDisplayedSession,
    errorMessage,
  });
  const { markSessionCompletionRead, refreshAgentActivity, syncAgentActivity } =
    activity;
  const queue = createChatQueue({
    chatState,
    operations,
    isDisplayedSession,
    errorMessage,
    onRestartQueueDiscarded,
  });
  const {
    refreshSessionQueue,
    syncSessionQueue,
    steerQueued,
    removeQueued,
    updateQueued,
    applyQueueInvalidation,
  } = queue;
  const history = createChatHistory({
    chatState,
    runStream,
    operations,
    isDisplayedSession,
    reflections,
    syncSessionQueue,
    errorMessage,
  });
  const {
    loadHistoryForSession,
    reconcileRunSession,
    loadOlderHistory,
    loadRunHistory,
    syncDisplayedHistoryLoad,
  } = history;
  let lastHistoryInvalidationId = null;

  // Apply App's Session invalidation window: activity first, then the History
  // of a displayed Session that gained records outside any Run (a goal row),
  // which no Run event delivers.
  function applySessionInvalidations(entries) {
    activity.applySessionInvalidations(entries);
    const { targets, lastId } = takeSessionInvalidations(
      entries,
      lastHistoryInvalidationId,
    );
    lastHistoryInvalidationId = lastId;
    const reread = new Set();
    for (const target of targets) {
      if (!target.historyAppended) continue;
      const sessionState =
        chatState.sessions[sessionKey(target.agentAddress, target.sessionId)];
      if (
        sessionState?.historyLoaded &&
        !reread.has(sessionState.key) &&
        isDisplayedSession(target.agentAddress, target.sessionId)
      ) {
        reread.add(sessionState.key);
        void loadHistoryForSession(target.agentAddress, target.sessionId);
      }
    }
  }

  function errorMessage(error) {
    return typeof error?.message === 'string' && error.message
      ? error.message
      : String(error ?? '');
  }

  async function loadAgents({ preferredAgentId = '', silent = false } = {}) {
    // `silent` skips the loadingAgents flag so a background refresh (triggered
    // by resource_changed(kind="agents")) does not tear down the entire chat
    // view via the {#if loadingAgents} conditional, and skips the initial
    // history load that belongs to the mount path. A newer silent refresh
    // inherits that initial load if the mount request is still in flight.
    const requestVersion = ++agentsLoadVersion;
    if (!silent) {
      chatState.loadingAgents = true;
      initialHistoryPending = true;
    }
    chatState.agentsError = null;
    let selectedAgentId;
    try {
      const result = await operations.listAgents();
      if (requestVersion !== agentsLoadVersion) return false;
      const preferred = chatState.selectedAgentId || preferredAgentId;
      if (preferred) {
        selectAgent(chatState, preferred);
      }
      selectedAgentId = setAgents(chatState, result.agents, {
        preserveSessionSelection,
      });
      onAgentsChanged(result.agents);
      if (selectedAgentId) {
        onAgentSelected(selectedAgentId);
      }
    } catch (error) {
      if (requestVersion !== agentsLoadVersion) return false;
      chatState.agentsError = errorMessage(error);
      return false;
    } finally {
      if (requestVersion === agentsLoadVersion) {
        chatState.loadingAgents = false;
      }
    }
    const loadInitialHistory = initialHistoryPending;
    initialHistoryPending = false;
    if (selectedAgentId && loadInitialHistory && shouldLoadCurrentHistory()) {
      await loadCurrentHistory();
    }
    return true;
  }

  // An Identity Agent was renamed: its Sessions keep their ids and History
  // under the new address. Their Run streams close (a rename admits no
  // active Run); requests still in flight for the old address are superseded.
  function renameAgent(oldAgentId, newAgentId) {
    if (!oldAgentId || !newAgentId || oldAgentId === newAgentId) {
      return;
    }
    // Deferred Run events still name the old address.
    applyRunEvents();
    const renamed = Object.values(chatState.sessions).filter(
      (sessionState) => sessionState.agentId === oldAgentId,
    );
    const oldKeys = renamed.map((sessionState) => sessionState.key);
    for (const key of oldKeys) {
      runStream.closeSubscriptionFor(key);
    }
    renameAgentInState(chatState, oldAgentId, newAgentId);
    renamed.forEach((sessionState, index) => {
      history.renameSession(oldKeys[index], sessionState.key);
      queue.renameSession(oldKeys[index], sessionState.key);
      reflections.renameSession(oldKeys[index], sessionState.key);
    });
  }

  async function loadCurrentHistory() {
    syncDisplayedHistoryLoad();
    const agent = selectedAgent(chatState);
    if (!agent?.current_session_id) {
      return false;
    }
    return loadHistoryForSession(agent.id, agent.current_session_id);
  }

  // A selection adopted without navigation still shows its current History.
  // While the mount's roster load is pending, that load reads the History
  // itself; a Chat mounted hidden has already finished it for another Agent.
  async function loadAdoptedSelectionHistory() {
    if (initialHistoryPending || !shouldLoadCurrentHistory()) {
      return false;
    }
    return loadCurrentHistory();
  }

  // A Session Compaction Policy saved from this window takes effect at once;
  // other windows reconcile it with their invalidated Session settings row.
  function applySessionCompactionPolicy(agentId, sessionId, policy) {
    const sessionState = chatState.sessions[sessionKey(agentId, sessionId)];
    if (sessionState && isRecord(policy)) {
      sessionState.compactionPolicy = policy;
    }
  }

  // The commands and Skills offered for `agentAddress`: in its Session
  // `sessionId`, or in the Project a new Session would work in
  // (`workingProjectId`, `null` for the Workspace; absent: the Agent's
  // default Project).
  async function loadCommands(
    agentAddress,
    { sessionId = '', workingProjectId } = {},
  ) {
    const requestVersion = ++commandsLoadVersion;
    chatState.commandsError = '';
    try {
      const params = agentAddress ? { agent_id: agentAddress } : {};
      if (agentAddress && sessionId) {
        params.session_id = sessionId;
      } else if (agentAddress && workingProjectId !== undefined) {
        params.working_project_id = workingProjectId;
      }
      const result = await operations.listChatCommands(params);
      if (requestVersion !== commandsLoadVersion) {
        return false;
      }
      chatState.availableSkills = result.items
        .filter(
          (item) => typeof item?.name === 'string' && item.name.length > 0,
        )
        .map((item) => ({
          name:
            item.type === 'command'
              ? normalizeBuiltInCommandName(item.name)
              : item.name,
          description: item.description ?? '',
          type: item.type,
          argument: item.argument,
          argument_hint: item.argument_hint ?? '',
          output: item.output,
          // False for a Skill a command of its name shadows: `/name` runs
          // the command, so the Skill is offered only under `$`.
          slash: item.slash !== false,
        }))
        .filter((item) => item.name.length > 0);
      return true;
    } catch (error) {
      if (requestVersion !== commandsLoadVersion) {
        return false;
      }
      chatState.commandsError = `${t('chat.skillsLoadError')} ${errorMessage(error)}`;
      chatState.availableSkills = [];
      return false;
    }
  }

  // Widget states ride only on a message to an existing Session, whose
  // widgets they come from; the server refuses them with a command, so a
  // send naming a known command carries none. An accepted message delivers
  // them, so they stop counting as changed; a command or a refused send
  // leaves them pending.
  async function sendMessage(sessionState, content, options = {}) {
    if (!sessionState) {
      return { kind: 'ignored' };
    }
    sessionState.actionError = '';
    const previousRunId = sessionState.currentRun?.runId;
    const widgetStates =
      Array.isArray(options.widgetStates) &&
      !namesKnownCommand(content, chatState.availableSkills)
        ? options.widgetStates
        : [];
    try {
      const run = await submitChat(`${submissionScope}:${sessionState.key}`, {
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        ...messageParams(content, options),
        ...(widgetStates.length > 0 ? { widget_states: widgetStates } : {}),
      });
      if (run?.command_handled) {
        return commandOutcome(run, sessionState.agentId);
      }
      markWidgetStatesSent(sessionState.key, widgetStates);
      return await admitSend(sessionState, run, previousRunId);
    } catch (error) {
      sessionState.actionError = `${t('chat.sendError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  // The first send from a draft (an Agent shown without a Session) creates
  // its Session: `new_session` asks the server to create it, make it an
  // Identity Agent's current Session, and handle the message there. This is
  // the one place a draft becomes request parameters. The created Session is
  // announced through `onSessionCreated` before its Run attaches, so the view
  // can show it in the draft's place. A response without a Session id (a
  // command that needs no Session) leaves the draft in place.
  async function sendToNewSession(draft, content, options = {}) {
    const agentAddress = draft?.agentAddress ?? '';
    if (!agentAddress) {
      return { kind: 'ignored' };
    }
    chatState.actionError = '';
    try {
      const response = await submitChat(
        `${submissionScope}:${draft.key ?? `${agentAddress}::draft`}`,
        {
          agent_id: agentAddress,
          new_session: newSessionParams(draft),
          ...messageParams(content, options),
        },
      );
      if (response?.accepted === true && response.session_available === false) {
        return {
          kind: 'accepted',
          runId: response.run_id ?? '',
          sessionState: null,
        };
      }
      const sessionId = createdSessionId(response);
      const sessionState = sessionId
        ? ensureSessionState(chatState, agentAddress, sessionId)
        : null;
      if (sessionState) {
        onSessionCreated(sessionState);
      }
      if (response?.command_handled) {
        if (sessionState && isDisplayedSession(agentAddress, sessionId)) {
          void loadHistoryForSession(agentAddress, sessionId);
        }
        return {
          ...commandOutcome(response, agentAddress, { navigation: false }),
          sessionState,
        };
      }
      if (!sessionState) {
        chatState.actionError = t('chat.sendError');
        return { kind: 'failed' };
      }
      return { ...(await admitSend(sessionState, response)), sessionState };
    } catch (error) {
      chatState.actionError = `${t('chat.sendError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  // Apply an accepted message to its Session: a queued item joins the Queue,
  // a started Run attaches while the Session is displayed.
  async function admitSend(sessionState, run, previousRunId = undefined) {
    if (run?.accepted === true && !run.sse_url) {
      if (run.session_available !== false) {
        await Promise.all([
          syncSessionQueue(sessionState),
          loadHistoryForSession(sessionState.agentId, sessionState.sessionId),
        ]);
      }
      return { kind: 'accepted', runId: run.run_id ?? '' };
    }
    if (run?.queued === true) {
      // The item may already have run while its send response was in flight.
      // Read the current Queue; the admission response cannot establish membership.
      await syncSessionQueue(sessionState);
      return { kind: 'queued' };
    }
    // Live admission may already have moved on to a successor Run.
    const currentRunId = sessionState.currentRun?.runId;
    if (
      currentRunId &&
      currentRunId !== run.run_id &&
      currentRunId !== previousRunId
    ) {
      return { kind: 'started', runId: run.run_id ?? '' };
    }
    startRun(sessionState, run);
    if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
      runStream.subscribeToRun(sessionState, run.sse_url, {
        afterSequence: 0,
      });
    }
    return { kind: 'started', runId: run.run_id ?? '' };
  }

  async function editMessage(sessionState, messageId, content) {
    if (!sessionState || !messageId) {
      return { kind: 'ignored' };
    }
    sessionState.actionError = '';
    const previousRunId = sessionState.currentRun?.runId;
    try {
      const run = await operations.editChatMessage({
        agent_id: sessionState.agentId,
        session_id: sessionState.sessionId,
        message_id: messageId,
        content,
      });
      const currentRunId = sessionState.currentRun?.runId;
      if (
        currentRunId &&
        currentRunId !== run.run_id &&
        currentRunId !== previousRunId
      ) {
        // The edited Run can finish and admit its successor before this RPC
        // returns. Reconcile the new lineage without truncating that Run's
        // live events or replacing its subscription with the predecessor.
        await reconcileRunSession(sessionState, currentRunId);
        return { kind: 'started', runId: run.run_id ?? '' };
      }
      truncateSessionForEdit(sessionState, messageId, run.run_id);
      startRun(sessionState, run);
      if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
        runStream.subscribeToRun(sessionState, run.sse_url, {
          afterSequence: 0,
        });
      }
      return { kind: 'started', runId: run.run_id ?? '' };
    } catch (error) {
      sessionState.actionError = `${t('chat.editError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    }
  }

  async function cancelActiveRun(sessionState) {
    const runId = sessionState?.currentRun?.runId;
    if (!runId || sessionState.cancellingRunIds.includes(runId)) {
      return;
    }
    sessionState.cancellingRunIds.push(runId);
    sessionState.actionError = '';
    try {
      const run = await operations.cancelRun(runId, { reason: 'user' });
      runStream.mergeRunResponse(sessionState, run);
      await reconcileRunSession(sessionState, runId);
    } catch (error) {
      if (sessionState.currentRun?.runId === runId)
        sessionState.actionError = `${t('chat.cancelError')} ${errorMessage(error)}`;
    } finally {
      sessionState.cancellingRunIds = sessionState.cancellingRunIds.filter(
        (pendingId) => pendingId !== runId,
      );
    }
  }

  // Stops everything the Session runs: its Run, background commands and
  // terminals, and every Sub-Agent below it. Each stopped Run ends through its
  // own live events; the outcome only reports how much was stopped.
  async function stopAll(sessionState) {
    if (!sessionState?.agentId || !sessionState?.sessionId) {
      return { kind: 'unavailable' };
    }
    if (sessionState.stoppingAll) {
      return { kind: 'pending' };
    }
    sessionState.stoppingAll = true;
    sessionState.actionError = '';
    try {
      const result = await operations.stopAll(
        sessionState.agentId,
        sessionState.sessionId,
      );
      const stopped = Number(result?.stopped);
      return {
        kind: 'stopped',
        stopped: Number.isFinite(stopped) && stopped > 0 ? stopped : 0,
      };
    } catch (error) {
      sessionState.actionError = `${t('chat.stopAllError')} ${errorMessage(error)}`;
      return { kind: 'failed' };
    } finally {
      sessionState.stoppingAll = false;
      work.refresh(sessionState);
    }
  }

  async function controlRun(sessionState, action, { runId, toolCallId } = {}) {
    const currentRun = sessionState?.currentRun;
    const targetRunId = runId ?? currentRun?.runId;
    if (
      !currentRun ||
      currentRun.runId !== targetRunId ||
      currentRun.status !== 'running'
    )
      return;
    const pendingKey = toolCallId ?? action;
    sessionState.pendingRunControls ??= {};
    if (sessionState.pendingRunControls[pendingKey]) return;
    sessionState.pendingRunControls[pendingKey] = true;
    sessionState.actionError = '';
    try {
      const run = await operations.controlRun({
        agentId: sessionState.agentId,
        sessionId: sessionState.sessionId,
        runId: targetRunId,
        action,
        toolCallId,
      });
      runStream.mergeRunResponse(sessionState, run);
    } catch (error) {
      sessionState.actionError = `${t('chat.controlRunError')} ${errorMessage(error)}`;
    } finally {
      delete sessionState.pendingRunControls[pendingKey];
    }
  }

  // The complete arguments and result of one Tool row, read in the History
  // generation the row was projected in.
  function loadToolCall(sessionState, { runId, toolId }) {
    return operations.loadToolCall({
      agent_id: sessionState.agentId,
      session_id: sessionState.sessionId,
      run_id: runId,
      tool_id: toolId,
      history_generation: sessionState.historyGeneration,
    });
  }

  async function cancelTool({
    sessionState,
    agentId = '',
    runId,
    toolCallId,
  } = {}) {
    if (!runId || !toolCallId) {
      return;
    }
    if (sessionState) {
      sessionState.actionError = '';
    }
    try {
      await operations.cancelToolCall({ agentId, runId, toolCallId });
    } catch (error) {
      if (sessionState) {
        sessionState.actionError = `${t('chat.cancelError')} ${errorMessage(error)}`;
      }
    }
  }

  function applyConnectionSnapshot(snapshot) {
    if (!snapshot || snapshot === handledConnectionSnapshot) {
      return false;
    }
    // Run events that arrived before the snapshot come first. The snapshot
    // itself recovers any of them this owner missed.
    applyRunEvents({ onMissed: () => {} });
    handledConnectionSnapshot = snapshot;
    queue.applyConnectionSnapshot(snapshot);
    // A replay gap or a server restart may have lost the end of a background
    // command. Its live state starts over (App's map with it), and the
    // displayed Session reads its History on: the read reports every live
    // command, so a durable `running` it leaves out has ended. The other
    // Sessions read on when shown.
    const commandEndsMissed =
      snapshot.replay_status === 'gap' ||
      snapshot.replay_status === 'epoch_changed';
    if (commandEndsMissed) {
      childTasks.forgetLearnedCommandStatuses();
    }
    runStream.applyConnectionSnapshot(snapshot);
    for (const sessionState of Object.values(chatState.sessions)) {
      if (!isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
        continue;
      }
      if (commandEndsMissed && sessionState.historyLoaded) {
        // Only a read without a cursor (in full) also reads the reflections.
        const readsInFull = !sessionState.historyAfter;
        void loadHistoryForSession(
          sessionState.agentId,
          sessionState.sessionId,
        );
        if (readsInFull) {
          continue;
        }
      }
      void reflections.refresh(sessionState);
    }
    return true;
  }

  // Apply the App's new Run events. A shown Chat applies each as it arrives.
  // A hidden one (`deferred`) leaves them in the feed: the App carries the
  // Runs of every Session, Swarm participants included, and nothing shows
  // them meanwhile. It applies them when shown, or before a completion fact
  // or activity read that must follow them. Its displayed Session's Run
  // starts still apply at once, so that Session streams as when shown.
  function handleRunEvents({ deferred = false } = {}) {
    if (deferred) {
      applyDisplayedRunStarts();
    } else {
      applyRunEvents();
    }
  }

  function applyRunEvents({ onMissed = recoverRunEvents } = {}) {
    if (!runEvents || runEventCursor >= runEvents.end) {
      return;
    }
    const { events, from, end, missed } = runEvents.read(runEventCursor);
    // Advance first: applying an event can lead back here.
    runEventCursor = end;
    runStartCursor = Math.max(runStartCursor, end);
    events.forEach((serverEvent, index) => {
      if (!earlyRunStarts.has(from + index)) {
        runStream.handleServerEvent(serverEvent);
      }
    });
    earlyRunStarts.clear();
    if (missed) {
      onMissed();
    }
  }

  function applyDisplayedRunStarts() {
    const after = Math.max(runEventCursor, runStartCursor);
    if (!runEvents || after >= runEvents.end) {
      return;
    }
    const { events, from, end } = runEvents.read(after);
    runStartCursor = end;
    events.forEach((serverEvent, index) => {
      const payload = serverEvent?.payload;
      if (
        serverEvent?.type === 'run_started' &&
        isDisplayedSession(
          formatAgentAddress(payload?.agent_id, payload?.project_id),
          payload?.session_id,
        )
      ) {
        earlyRunStarts.add(from + index);
        runStream.handleServerEvent(serverEvent);
      }
    });
  }

  // Run events left the feed before this owner applied them. Start again
  // from current state, as after a reconnect: the feed's active Runs,
  // durable completion activity, the Queues on show and the displayed
  // Session's reflections.
  function recoverRunEvents() {
    runStream.applyConnectionSnapshot({ active_runs: runEvents.activeRuns });
    activity.refreshAllActivity();
    for (const sessionState of Object.values(chatState.sessions)) {
      void refreshSessionQueue(sessionState);
      if (isDisplayedSession(sessionState.agentId, sessionState.sessionId)) {
        void reflections.refresh(sessionState);
      }
    }
  }

  // A Chat owner created after the app connected (split view's second area,
  // leaving onboarding, recovery remounts) starts from current server state:
  // the latest connection snapshot with the feed's live Run list. The events
  // already in the feed are reflected in that list and may have lost the
  // terminal events of Runs the snapshot named, so they are not replayed.
  function startFromServerState(connectionSnapshot) {
    if (!connectionSnapshot || !runEvents) {
      return false;
    }
    runEventCursor = runEvents.end;
    runStartCursor = runEventCursor;
    handledConnectionSnapshot = connectionSnapshot;
    // A new owner holds no Sessions yet, so the snapshot's Queue scopes have
    // nothing to reconcile; its Sessions load their Queues when opened.
    runStream.applyConnectionSnapshot({
      ...connectionSnapshot,
      active_runs: runEvents.activeRuns,
    });
    return true;
  }

  function destroy() {
    agentsLoadVersion += 1;
    initialHistoryPending = false;
    chatState.loadingAgents = false;
    runStream.closeSubscriptions();
    history.destroy();
    queue.destroy();
    childTasks.dispose();
    activity.dispose();
    work.destroy();
  }

  return {
    applyCommandStatuses,
    applyConnectionSnapshot,
    applyQueueInvalidation,
    applySessionCompactionPolicy,
    applySessionInvalidations,
    applySubAgentStatusUpdates,
    cancelActiveRun,
    cancelSubAgent,
    cancelTool,
    controlRun,
    destroy,
    editMessage,
    handleRunEvents,
    startFromServerState,
    listFiles: (agentAddress, target) =>
      operations.listFiles(agentAddress, target),
    getSession: (...args) => operations.getSession(...args),
    getSessionChangeStats: (...args) =>
      operations.getSessionChangeStats(...args),
    getSessionUsage: (...args) => operations.getSessionUsage(...args),
    getSubAgentTree: (...args) => operations.getSubAgentTree(...args),
    listSessions: (...args) => operations.listSessions(...args),
    loadAdoptedSelectionHistory,
    loadAgents,
    loadCommandOutput,
    loadCommands,
    loadCurrentHistory,
    syncDisplayedHistoryLoad,
    loadHistoryForSession,
    loadOlderHistory,
    loadRunHistory,
    loadToolCall,
    loadReflectionChanges: reflections.loadChanges,
    loadSubAgentWork,
    loadProject: (projectId) => operations.showProject(projectId),
    markSessionCompletionRead,
    reconcileRunSession,
    reconcileSubAgentRows,
    refreshAgentActivity,
    refreshReflections: reflections.refresh,
    refreshSessionQueue,
    removeQueued,
    renameAgent,
    steerQueued,
    sendMessage,
    setSessionAgentOverrides: (...args) =>
      operations.setSessionAgentOverrides(...args),
    sendToNewSession,
    stopAll,
    stopCommand,
    stopSubAgentWork,
    syncAgentActivity,
    syncSessionWork: work.sync,
    syncSessionQueue,
    undoReflection: reflections.undo,
    updateQueued,
  };
}

// Whether `content` starts with a command of the loaded catalog, the way
// the server recognizes one: its only text begins with `/<name>`.
function namesKnownCommand(content, availableSkills) {
  const texts =
    typeof content === 'string'
      ? [content]
      : (Array.isArray(content) ? content : [])
          .filter((block) => block?.type === 'text')
          .map((block) => block.text);
  const text = texts.length === 1 ? String(texts[0] ?? '').trim() : '';
  if (!text.startsWith('/')) {
    return false;
  }
  const name = text.slice(1).split(/\s/, 1)[0].toLowerCase();
  return (Array.isArray(availableSkills) ? availableSkills : []).some(
    (item) => item?.type === 'command' && item.name === name,
  );
}

function normalizeBuiltInCommandName(value) {
  if (typeof value !== 'string') {
    return '';
  }
  return value.trim().replace(/^\/+/, '').toLowerCase();
}

// What a draft asks of its new Session: the Working Project it chose
// (`workingProjectId`, `null` for the Workspace; absent: the Agent's default
// Project) and the Agent overrides it holds (`agentOverrides`).
function newSessionParams(draft) {
  const params = {};
  if (draft.workingProjectId !== undefined) {
    params.working_project_id = draft.workingProjectId;
  }
  const overrides = Object.fromEntries(
    Object.entries(
      isRecord(draft.agentOverrides) ? draft.agentOverrides : {},
    ).filter(
      ([, value]) => value !== undefined && value !== null && value !== '',
    ),
  );
  if (Object.keys(overrides).length > 0) {
    params.agent_overrides = overrides;
  }
  return params;
}

function messageParams(content, options = {}) {
  const params = { content };
  if (options.inputOrigin) {
    params.input_origin = options.inputOrigin;
  }
  if (Array.isArray(options.fileMentions) && options.fileMentions.length > 0) {
    params.file_mentions = options.fileMentions;
  }
  return params;
}

// The outcome a handled slash command asks Chat to present. Only a command
// sent in an existing Session can navigate: from a draft, the server answers
// navigation commands with a notice.
function commandOutcome(response, agentAddress, { navigation = true } = {}) {
  const extensionNavigation = response?.data?.navigation;
  if (
    response.output === 'action' &&
    extensionNavigation?.kind === 'open_extension_page' &&
    typeof extensionNavigation.extension === 'string' &&
    typeof extensionNavigation.page === 'string' &&
    typeof extensionNavigation.route === 'string'
  ) {
    return { kind: 'extension_page', navigation: extensionNavigation };
  }
  if (navigation) {
    const move = resolveMoveActionFromResponse(response);
    if (move) {
      return { kind: 'move', move };
    }
    const sessionSwitch = commandSwitchFromResponse(response);
    const { projectId } = parseAgentAddress(agentAddress);
    if (sessionSwitch && !projectId) {
      return { kind: 'switch', sessionSwitch };
    }
    // `/new` names no Session: the next conversation starts as a draft.
    if (
      response.data?.command === 'new' &&
      !trimmedText(response.data.session_id)
    ) {
      return { kind: 'draft', agentAddress, reply: response.reply };
    }
  }
  // `/model <value|reset>` wrote the Agent's Model (`data.model`) and cleared
  // the Session's own Model; bare `/model` only reports it.
  const modelChange =
    response.data?.command === 'model' &&
    typeof response.data.model === 'string'
      ? { modelChanged: true }
      : {};
  if (response.output === 'transient') {
    return { kind: 'transient', reply: response.reply, ...modelChange };
  }
  return { kind: 'toast', reply: response.reply, ...modelChange };
}

function trimmedText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

// The Session a draft's send created: a Run or queued response names it at
// its top level, a handled command at its top level or in its data.
function createdSessionId(response) {
  if (!isRecord(response)) {
    return '';
  }
  return (
    trimmedText(response.session_id) ||
    (response.command_handled === true
      ? trimmedText(response.data?.session_id)
      : trimmedText(response.item?.session_id))
  );
}

function commandSwitchFromResponse(response) {
  const data = response?.data;
  const sessionId = trimmedText(data?.session_id);
  if (!sessionId || data.command !== 'handoff') {
    return null;
  }
  const targetAgentId = trimmedText(data.agent_id);
  return { sessionId, targetAgentId };
}
