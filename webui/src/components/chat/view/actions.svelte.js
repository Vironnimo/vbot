import { onDestroy } from 'svelte';
import {
  isRunActive,
  visibleTimelineItemsForRender,
  selectAgent,
} from '../../../lib/chatState.js';
import {
  extractMentionTokens,
  matchMentionCandidates,
} from '$lib/fileMentions.js';

export function createChatViewActions(context) {
  // Chat-local bottom toast for transient command replies and lifecycle notices.
  // Operation errors stay beside the composer until their owner clears them.
  let chatToast = $state('');

  // Non-persisted `output: "transient"` command cards (/status, /help) rendered
  // in the chat stream. Kept in a dedicated array so incoming run events never
  // clear them; only a displayed-session change (or reload) empties them. Each
  // card carries the id of the timeline item it followed at creation, so the
  // timeline anchors it in place instead of restacking all cards at the bottom.
  let transientCards = $state([]);

  let transientCardsSessionKey = '';

  let displayedSessionGeneration = 0;

  let generationSessionKey = '';

  let transientCardSeq = 0;

  let submittedTurnScrollKey = $state(0);

  // Bottom command toast auto-dismiss. Kept as a single constant so the
  // dwell time can be tuned in one place.
  const CHAT_TOAST_TIMEOUT_MS = 5000;

  let chatToastTimeoutId = null;

  onDestroy(() => {
    if (chatToastTimeoutId !== null) {
      clearTimeout(chatToastTimeoutId);
      chatToastTimeoutId = null;
    }
  });

  // Transient cards belong to the displayed session only. Switching sessions
  // (or the page reloading) drops them; reloading the same session's history
  // (e.g. after /compact) does not, because the displayed key is unchanged.
  $effect(() => {
    const key = context.target.displayedSessionKey();
    if (key !== generationSessionKey) {
      generationSessionKey = key;
      displayedSessionGeneration += 1;
    }
    if (key !== transientCardsSessionKey) {
      transientCardsSessionKey = key;
      transientCards = [];
    }
  });

  const showChatToast = (message) => {
    if (chatToastTimeoutId !== null) {
      clearTimeout(chatToastTimeoutId);
      chatToastTimeoutId = null;
    }

    chatToast = typeof message === 'string' ? message : '';

    if (!chatToast) {
      return;
    }

    chatToastTimeoutId = setTimeout(() => {
      chatToast = '';
      chatToastTimeoutId = null;
    }, CHAT_TOAST_TIMEOUT_MS);
  };

  const clearSessionActionError = (
    sessionState = context.target.activeSessionState,
  ) => {
    context.chatState.actionError = '';
    if (sessionState) {
      sessionState.actionError = '';
    }
  };

  const setSessionActionError = (
    message,
    sessionState = context.target.activeSessionState,
  ) => {
    if (sessionState) {
      sessionState.actionError = message;
      return;
    }
    context.chatState.actionError = message;
  };

  const appendTransientCard = (text, sessionState) => {
    const body = typeof text === 'string' ? text : '';
    if (!body) {
      return;
    }
    // Anchor the card to the timeline item present when the command ran, so it
    // stays at that position (like a chat message) instead of being pushed to
    // the bottom by later messages. `null` anchors a card created on an empty
    // timeline to the top. `createdAt` is the fallback anchor: when a history
    // reload replaces the anchor item's live id, the card keeps its
    // chronological position by creation time instead of sinking to the end.
    const items = visibleTimelineItemsForRender(sessionState);
    const anchorId = items.length > 0 ? items[items.length - 1].id : null;
    transientCardSeq += 1;
    transientCards = [
      ...transientCards,
      {
        id: `transient-${transientCardSeq}`,
        text: body,
        anchorId,
        createdAt: Date.now(),
      },
    ];
  };

  const handleTranscriptionError = (message) => {
    setSessionActionError(message);
  };

  const sendStream = async (agent, sessionState, content, options = {}) => {
    const sourceSessionKey = sessionState?.key ?? '';
    const sourceUiGeneration = displayedSessionGeneration;
    const outcome = await context.chatController.sendMessage(
      sessionState,
      content,
      options,
    );
    const presentationIsCurrent =
      sourceSessionKey &&
      context.target.displayedSessionKey() === sourceSessionKey &&
      displayedSessionGeneration === sourceUiGeneration;
    if (!presentationIsCurrent) {
      return outcome.kind !== 'failed' && outcome.kind !== 'ignored';
    }
    if (outcome.kind === 'move') {
      await context.navigation.moveSessionToAgent(outcome.move);
      context.layout.requestComposerFocus({ includeMobile: true });
    } else if (outcome.kind === 'switch') {
      const targetAgentId = outcome.sessionSwitch.targetAgentId || agent.id;
      if (targetAgentId !== context.chatState.selectedAgentId) {
        selectAgent(context.chatState, targetAgentId);
        context.onAgentSelected?.(targetAgentId);
      }
      await context.navigation.switchToCurrentSession(
        targetAgentId,
        outcome.sessionSwitch.sessionId,
      );
      context.layout.requestComposerFocus({ includeMobile: true });
    } else if (outcome.kind === 'extension_page') {
      window.dispatchEvent(
        new CustomEvent('vbot-extension-page', { detail: outcome.navigation }),
      );
    } else if (outcome.kind === 'transient') {
      appendTransientCard(outcome.reply, sessionState);
    } else if (outcome.kind === 'toast') {
      showChatToast(outcome.reply);
    } else if (outcome.kind === 'started') {
      submittedTurnScrollKey += 1;
    }
    return outcome.kind !== 'failed' && outcome.kind !== 'ignored';
  };

  // Sessions whose manual `/compact` submission is still being admitted.
  let compactionSubmissions = $state([]);

  // The context card's Compaction action. An active Run takes the request at
  // its next safe step; otherwise the server-owned manual `/compact` command
  // starts its own Run through the ordinary send path (no local state).
  const handleCompactContext = async () => {
    const agent = context.target.activeAgent;
    const sessionState = context.target.activeSessionState;
    if (!agent || !sessionState) {
      return;
    }
    if (isRunActive(sessionState)) {
      await context.chatController.controlRun(sessionState, 'compact');
      return;
    }
    const key = sessionState.key ?? '';
    if (compactionSubmissions.includes(key)) {
      return;
    }
    compactionSubmissions = [...compactionSubmissions, key];
    try {
      await sendStream(agent, sessionState, '/compact');
    } finally {
      compactionSubmissions = compactionSubmissions.filter(
        (pendingKey) => pendingKey !== key,
      );
    }
  };

  const isCompactionSubmitting = (sessionState) =>
    Boolean(sessionState?.pendingRunControls?.compact) ||
    compactionSubmissions.includes(sessionState?.key ?? '');

  const handleEditMessage = async (messageId, content) => {
    const sessionState = context.target.activeSessionState;
    const sourceSessionKey = sessionState?.key ?? '';
    const sourceUiGeneration = displayedSessionGeneration;
    const outcome = await context.chatController.editMessage(
      sessionState,
      messageId,
      content,
    );
    const presentationIsCurrent =
      sourceSessionKey &&
      context.target.displayedSessionKey() === sourceSessionKey &&
      displayedSessionGeneration === sourceUiGeneration;
    if (presentationIsCurrent && outcome.kind === 'started') {
      submittedTurnScrollKey += 1;
    }
    return outcome.kind === 'started';
  };

  const handleCancelRun = async () => {
    await context.chatController.cancelActiveRun(
      context.target.activeSessionState,
    );
  };

  // Per-tool-call cancel: cancel the bash without aborting the owning run.
  const handleCancelToolCall = async ({ runId, toolCallId } = {}) => {
    const agent = context.target.activeAgent;
    await context.chatController.cancelTool({
      sessionState: context.target.activeSessionState,
      agentId: agent?.id ?? '',
      runId,
      toolCallId,
    });
  };

  const handleCancelSubAgent = async ({ tool } = {}) => {
    await context.chatController.cancelSubAgent({
      tool,
      sessionState: context.target.activeSessionState,
      projectId: context.target.displayedSessionProjectId(),
    });
  };

  const handleCancelBackgroundProcess = async ({ processId } = {}) => {
    await context.chatController.cancelBackgroundProcess({
      sessionState: context.target.activeSessionState,
      agentId:
        context.target.activeSessionState?.agentId ??
        context.target.activeAgent?.id ??
        '',
      processId,
      projectId: context.target.displayedSessionProjectId(),
    });
  };

  const handleSteerQueuedMessage = async (queuedMessageId) => {
    const sessionState = context.target.activeSessionState;
    return await context.chatController.steerQueued(
      sessionState,
      queuedMessageId,
    );
  };

  const handleRemoveQueuedMessage = async (queuedMessageId) => {
    const sessionState = context.target.activeSessionState;
    await context.chatController.removeQueued(sessionState, queuedMessageId);
  };

  const handleEditQueuedMessage = async (queuedMessageId, newContent) => {
    const sessionState = context.target.activeSessionState;
    if (!sessionState) {
      return false;
    }
    const fileMentions = await collectQueueEditFileMentions(
      newContent,
      sessionState.agentId,
    );
    return await context.chatController.updateQueued(
      sessionState,
      queuedMessageId,
      newContent,
      fileMentions,
    );
  };

  const collectQueueEditFileMentions = async (text, agentAddress) => {
    const tokens = extractMentionTokens(typeof text === 'string' ? text : '');
    if (tokens.length === 0) {
      return [];
    }
    try {
      if (!agentAddress) {
        return [];
      }
      const result = await context.chatController.listFiles(agentAddress);
      return matchMentionCandidates(
        tokens,
        Array.isArray(result?.files) ? result.files : [],
      );
    } catch {
      // Without a file list nothing can be verified as a mention; the edit
      // still goes through as plain text.
      return [];
    }
  };
  const loadCurrentHistory = () => {
    context.chatState.actionError = '';
    return context.chatController.loadCurrentHistory();
  };

  // Load a session's history by its outside agent spelling (bare id for an
  // identity session, `agent@projekt` for a project-agent session) — one path
  // for both worlds, since `chat.history` parses the address (trap 2).
  //
  // Stale-response discipline: the displayed session can change while
  // `chat.history` is in flight (rapid switching, fast Back/Forward). After
  // every await, per-session state may always be written (each response lands
  // in its own session state), but global UI state (`context.chatState.loadingHistory`,
  // `context.chatState.historyError`) and the SSE stream attach belong to the DISPLAYED session
  // only — a stale response must not re-open a subscription the newer
  // navigation just closed, unlock the composer early, or banner-error a
  // healthy session.
  const loadHistoryForSession = (agentId, sessionId) => {
    context.chatState.actionError = '';
    return context.chatController.loadHistoryForSession(agentId, sessionId);
  };

  const loadOlderHistory = () =>
    context.chatController.loadOlderHistory(context.target.activeSessionState);

  return {
    loadCurrentHistory,
    loadHistoryForSession,
    loadOlderHistory,
    get chatToast() {
      return chatToast;
    },
    get transientCards() {
      return transientCards;
    },
    get submittedTurnScrollKey() {
      return submittedTurnScrollKey;
    },
    get showChatToast() {
      return showChatToast;
    },
    get clearSessionActionError() {
      return clearSessionActionError;
    },
    get setSessionActionError() {
      return setSessionActionError;
    },
    get handleTranscriptionError() {
      return handleTranscriptionError;
    },
    get sendStream() {
      return sendStream;
    },
    get handleEditMessage() {
      return handleEditMessage;
    },
    get handleCompactContext() {
      return handleCompactContext;
    },
    get isCompactionSubmitting() {
      return isCompactionSubmitting;
    },
    get handleCancelRun() {
      return handleCancelRun;
    },
    get handleCancelToolCall() {
      return handleCancelToolCall;
    },
    get handleCancelSubAgent() {
      return handleCancelSubAgent;
    },
    get handleCancelBackgroundProcess() {
      return handleCancelBackgroundProcess;
    },
    get handleSteerQueuedMessage() {
      return handleSteerQueuedMessage;
    },
    get handleRemoveQueuedMessage() {
      return handleRemoveQueuedMessage;
    },
    get handleEditQueuedMessage() {
      return handleEditQueuedMessage;
    },
  };
}
