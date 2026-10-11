import { visibleTimelineItemsForRender } from '../../../../webui/src/lib/chatTimeline.js';
import {
  createChatState,
  ensureSessionState,
  appendRunEvent,
  loadHistory,
  TERMINAL_RUN_EVENTS,
} from '../../../../webui/src/lib/chatState.js';
import { untrack } from 'svelte';
import { failureText } from './failureText.js';

// Why an interrupted Run stopped, as its summary's completion reason names
// it; another reason counts as a plain interruption.
const INTERRUPTION_REASONS = new Set([
  'network',
  'timeout',
  'provider',
  'internal',
  'shutdown',
  'process_restart',
]);

export function createSwarmPageActivity(host) {
  let history = $state(null);

  const projection = $state(
    ensureSessionState(createChatState(), 'swarm', 'activity'),
  );
  let reasoningOpen = $state({});
  // The participant whose Activity shows or loads; its header shows before
  // its History arrives.
  let inspectedParticipant = $state.raw(null);
  let inspectionRequest = null;
  // eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
  const settledRuns = new Set();
  let flushTimer = null;
  let pendingEvents = [];
  // Activity reads History and follows its participant's Run only while its
  // tab shows. A hidden Activity keeps what it shows and catches up once when
  // the tab shows again.
  let paused = false;
  // Whether a read for the inspected participant failed (its History, the
  // catalog or its Run's subscription), so a refresh inspects it again
  // instead of keeping what Activity shows.
  let stale = false;

  function flushEvents() {
    clearTimeout(flushTimer);
    flushTimer = null;
    const events = pendingEvents;
    pendingEvents = [];
    for (const event of events) appendRunEvent(projection, event);
  }

  // Drops the live projection of the followed Run, keeping loaded History.
  function clearLiveRun() {
    clearTimeout(flushTimer);
    flushTimer = null;
    pendingEvents = [];
    projection.runEvents = [];
    projection.streamingRunEvents = [];
    projection.streamingPhase = 0;
    // eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, as in Chat's own state
    projection.seenStreamingEventKeys = new Set();
    projection.currentRun = null;
    projection.status = 'idle';
  }

  function clearProjection() {
    clearLiveRun();
    projection.messages = [];
    projection.historyRuns = {};
    projection.historyGeneration = null;
    projection.historyAfter = '';
  }

  function adoptHistory(data) {
    loadHistory(projection, data.messages, {
      runs: data.runs,
      incremental: data.incremental,
      generation: data.history_generation,
      nextAfter: data.next_after,
      hasMore: data.has_more,
      nextBefore: data.next_before,
      sessionUsage: data.session_usage,
      contextUsage: data.context_usage,
    });
  }

  function isReasoningOpen(id) {
    return Boolean(reasoningOpen[id]);
  }

  function setReasoningOpen(id, open) {
    reasoningOpen[id] = open;
  }

  let currentSubscription = null;

  let activityRequest = 0;

  let historyRequest = 0;

  let historyPageCount = 1;

  let historyLoading = $state(false);

  function leaveActivity() {
    activityRequest += 1;
    historyRequest += 1;
    historyPageCount = 1;
    historyLoading = false;
    currentSubscription?.();
    currentSubscription = null;
    history = null;
    clearProjection();
    reasoningOpen = {};
    inspectedParticipant = null;
    inspectionRequest = null;
    paused = false;
    stale = false;
    settledRuns.clear();
  }

  // Shows a failed read for the inspected participant: one the page started
  // on its own (`background`) as a refresh failure, one the user started as
  // the page error.
  function readFailed(cause, background) {
    stale = true;
    if (background) host.model.refreshFailed(cause);
    else host.model.error = failureText(cause);
  }

  // Stops reading and following the inspected participant's Run while the
  // tab is hidden; reads in flight are dropped.
  function pause() {
    if (paused || inspectedParticipant === null) return;
    paused = true;
    if (flushTimer !== null) flushEvents();
    else pendingEvents = [];
    activityRequest += 1;
    historyRequest += 1;
    historyLoading = false;
    inspectionRequest = null;
    currentSubscription?.();
    currentSubscription = null;
  }

  // Catches up once with the participant's current snapshot: newer History,
  // and its Run if it is still active.
  function resume() {
    if (!paused) return;
    paused = false;
    const participant =
      host.model.selectedSwarm?.participants?.find(
        (item) => item.id === inspectedParticipant?.id,
      ) ?? inspectedParticipant;
    if (participant)
      void inspectParticipant(participant, {
        activate: false,
        preserve: true,
        catchUp: true,
      });
  }

  $effect(() => {
    if (host.model.activeTab === 'participants') untrack(resume);
    else untrack(pause);
  });

  const activityTimeline = $derived(
    history ? visibleTimelineItemsForRender(projection) : [],
  );

  // The inspected participant as the Run lists it now.
  const selectedParticipant = $derived(
    inspectedParticipant &&
      (host.model.selectedSwarm?.participants?.find(
        (item) => item.id === inspectedParticipant.id,
      ) ??
        inspectedParticipant),
  );

  const contextWindow = $derived(
    host.model.catalog.models?.find(
      (model) => model.id === selectedParticipant?.model,
    )?.context_window,
  );

  // Why the inspected participant stopped when it failed or was interrupted
  // and its loaded History does not say so itself. Its lifecycle Run
  // decides, since a participant also counts as failed after an interrupted
  // Run: the completion reason of an interrupted Run ('interrupted' when
  // unnamed), 'failed' for a failed Run whose error is not the last entry,
  // and for a Run that ended otherwise 'not_started' (its next Run could not
  // start) or 'interrupted'. Null while History has not caught up with the
  // Run.
  const stopReason = $derived.by(() => {
    const participant = selectedParticipant;
    if (
      !history ||
      !participant ||
      participant.run_active ||
      !['failed', 'interrupted'].includes(participant.state)
    )
      return null;
    const runId = participant.lifecycle_run_id;
    const run = runId ? projection.historyRuns[runId] : undefined;
    if (run?.status === 'running' || (!run && historyLoading)) return null;
    if (run?.status === 'interrupted') {
      const reason = activityTimeline.findLast(
        (item) => item.type === 'assistant_run' && item.runId === runId,
      )?.runSummaryMessage?.completion_reason;
      return INTERRUPTION_REASONS.has(reason) ? reason : 'interrupted';
    }
    if (run?.status === 'failed') {
      const last = activityTimeline.at(-1);
      return last?.type === 'message' && last.message.role === 'error'
        ? null
        : 'failed';
    }
    return participant.state === 'failed' ? 'not_started' : 'interrupted';
  });

  // Reads the participant's History, or with History shown what is newer.
  // `settled` marks a participant whose Run ended; `background` a read the
  // page started on its own.
  async function reconcileActivity(
    request,
    swarmId,
    participant,
    { settled = false, background = false } = {},
  ) {
    const read = ++historyRequest;
    const pageCount = historyPageCount;
    historyLoading = true;
    try {
      let data = await host.model.client.readHistory(swarmId, participant.id, {
        limit: 100,
        ...(history?.participant.id === participant.id &&
        projection.historyAfter
          ? { after: projection.historyAfter }
          : {}),
      });
      const pages = [data];
      while (
        !data.incremental &&
        pages.length < pageCount &&
        pages.at(-1).has_more &&
        pages.at(-1).next_before
      ) {
        if (
          host.model.disposed ||
          request !== activityRequest ||
          read !== historyRequest
        )
          return;
        pages.push(
          await host.model.client.readHistory(swarmId, participant.id, {
            limit: 100,
            before: pages.at(-1).next_before,
          }),
        );
      }
      while (data.incremental && data.has_newer && data.next_after) {
        if (
          host.model.disposed ||
          request !== activityRequest ||
          read !== historyRequest
        )
          return;
        const next = await host.model.client.readHistory(
          swarmId,
          participant.id,
          {
            limit: 100,
            after: data.next_after,
          },
        );
        if (!next.incremental) {
          pages.splice(0, pages.length, next);
          data = next;
          break;
        }
        pages.push(next);
        data = next;
      }
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      ) {
        const oldest = pages.at(-1);
        if (!data.incremental) historyPageCount = pages.length;
        history = {
          participant: settled
            ? { ...participant, run_active: false }
            : participant,
          data: {
            ...data,
            messages: (data.incremental ? pages : pages.toReversed()).flatMap(
              (page) => page.messages ?? [],
            ),
            runs: (data.incremental ? pages : pages.toReversed()).flatMap(
              (page) => page.runs ?? [],
            ),
            has_more: data.incremental
              ? history.data.has_more
              : oldest.has_more,
            next_before: data.incremental
              ? history.data.next_before
              : oldest.next_before,
          },
        };
        adoptHistory(history.data);
        history.data.messages = projection.messages;
        history.data.runs = Object.values(projection.historyRuns);
        history.data.incremental = false;
        if (settled) {
          if (participant.lifecycle_run_id)
            settledRuns.add(participant.lifecycle_run_id);
          inspectedParticipant = { ...participant, run_active: false };
          projection.status = data.status ?? 'completed';
          if (projection.currentRun?.runId === participant.lifecycle_run_id)
            projection.currentRun.status = projection.status;
        }
      }
    } catch (cause) {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        readFailed(cause, background);
    } finally {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        historyLoading = false;
    }
  }

  async function loadEarlierActivity() {
    if (!history?.data.has_more || !history.data.next_before || historyLoading)
      return;
    const request = activityRequest;
    const read = ++historyRequest;
    const selected = history;
    historyLoading = true;
    try {
      const data = await host.model.client.readHistory(
        host.model.selectedSwarm.id,
        selected.participant.id,
        {
          limit: 100,
          before: selected.data.next_before,
        },
      );
      if (
        host.model.disposed ||
        request !== activityRequest ||
        read !== historyRequest
      )
        return;
      historyPageCount += 1;
      history = {
        ...selected,
        data: {
          ...history.data,
          messages: [...(data.messages ?? []), ...selected.data.messages],
          runs: [...(data.runs ?? []), ...(selected.data.runs ?? [])],
          has_more: data.has_more,
          next_before: data.next_before,
        },
      };
      adoptHistory(history.data);
    } catch (cause) {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        host.model.error = failureText(cause);
    } finally {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        historyLoading = false;
    }
  }

  // `preserve` keeps the shown History and the followed Run while the
  // participant's Run identity and active state are unchanged and no read
  // for it failed; `catchUp` reconciles a resumed Activity even then. An
  // inspection the user starts opens the tab; one that does not `activate`
  // it is the page's own (a refresh, the tab showing again), and its failed
  // reads are refresh failures.
  async function inspectParticipant(
    participant,
    { activate = true, preserve = false, catchUp = false } = {},
  ) {
    if (!host.model.selectedSwarm) return;
    if (activate) host.model.openTab('participants');
    // A hidden Activity catches up when its tab shows again.
    else if (paused) return;
    const background = !activate;
    if (settledRuns.has(participant.lifecycle_run_id))
      participant = { ...participant, run_active: false };
    if (
      !catchUp &&
      !stale &&
      preserve &&
      inspectedParticipant?.id === participant.id &&
      inspectedParticipant.lifecycle_run_id === participant.lifecycle_run_id &&
      inspectedParticipant.run_active === participant.run_active &&
      (inspectionRequest === activityRequest ||
        historyLoading ||
        currentSubscription ||
        !participant.lifecycle_run_id ||
        (participant.run_active === false &&
          !projection.runEvents.some(
            (event) => event.run_id === participant.lifecycle_run_id,
          )))
    )
      return;
    if (preserve) {
      if (flushTimer !== null) flushEvents();
      else pendingEvents = [];
      activityRequest += 1;
      currentSubscription?.();
      currentSubscription = null;
      // A followed Run that ended or was replaced while the tab was hidden
      // left a live projection that missed its end; History holds that Run.
      if (
        catchUp &&
        projection.status === 'running' &&
        (projection.currentRun?.runId !== participant.lifecycle_run_id ||
          participant.run_active === false)
      )
        clearLiveRun();
    } else leaveActivity();
    inspectedParticipant = participant;
    stale = false;
    const request = activityRequest;
    const swarmId = host.model.selectedSwarm.id;
    inspectionRequest = request;
    await Promise.all([
      reconcileActivity(request, swarmId, participant, {
        settled: participant.run_active === false,
        background,
      }),
      host.model.catalog.models
        ? Promise.resolve()
        : host.model
            .call('catalog')
            .then((result) => {
              host.model.catalog = result.catalog;
            })
            .catch((cause) => {
              if (!host.model.disposed && request === activityRequest)
                readFailed(cause, background);
            }),
    ]);
    if (inspectionRequest === request) inspectionRequest = null;
    if (
      host.model.disposed ||
      request !== activityRequest ||
      !history ||
      !participant.lifecycle_run_id ||
      participant.run_active === false
    )
      return;
    projection.currentRun = {
      runId: participant.lifecycle_run_id,
      status: 'running',
    };
    projection.status = 'running';
    let key = null;
    const buffered = [];
    // eslint-disable-next-line svelte/prefer-svelte-reactivity -- bookkeeping, never rendered
    const sequences = new Set();
    let replayThrough = Infinity;
    let replayCaughtUp = false;
    const receive = (id, event) => {
      if (
        host.model.disposed ||
        request !== activityRequest ||
        id !== key ||
        event.run_id !== participant.lifecycle_run_id ||
        sequences.has(event.sequence)
      )
        return;
      sequences.add(event.sequence);
      pendingEvents.push(event);
      if (!replayCaughtUp && event.sequence >= replayThrough) {
        replayCaughtUp = true;
        flushEvents();
      } else if (replayCaughtUp && flushTimer === null) {
        // Match Chat's batched, compressed streaming projection.
        flushTimer = setTimeout(flushEvents, 33);
      }
      const payload = event.payload ?? {};
      if (
        history &&
        (payload.context_usage || payload.session_usage || payload.usage)
      ) {
        history = {
          ...history,
          data: {
            ...history.data,
            ...(payload.context_usage
              ? { context_usage: payload.context_usage }
              : {}),
            ...(payload.session_usage
              ? { session_usage: payload.session_usage }
              : {}),
            ...(payload.usage ? { usage: payload.usage } : {}),
          },
        };
      }
      if (TERMINAL_RUN_EVENTS.has(event.type)) {
        flushEvents();
        settledRuns.add(participant.lifecycle_run_id);
        inspectedParticipant = { ...participant, run_active: false };
        currentSubscription?.();
        currentSubscription = null;
        if (history)
          history.participant = { ...participant, run_active: false };
        // The Run ended on its own, so this read is the page's own.
        void reconcileActivity(request, swarmId, participant, {
          settled: true,
          background: true,
        });
      }
    };
    const off = host.model.client.onRunEvent((id, event) => {
      if (key === null) buffered.push([id, event]);
      else receive(id, event);
    });
    // A failed unsubscribe shows only while its inspection is current, which
    // leaves the page's own at the end of the Run (`receive`).
    const unsubscribe = (id) => {
      void host.model.client.unsubscribeRun(id).catch((cause) => {
        if (!host.model.disposed && request === activityRequest)
          host.model.refreshFailed(cause);
      });
    };
    currentSubscription = () => {
      off();
      if (key) unsubscribe(key);
    };
    try {
      const subscription = await host.model.client.subscribeRun(
        swarmId,
        participant.lifecycle_run_id,
      );
      if (!subscription.subscription_id) {
        off();
        if (request === activityRequest) {
          currentSubscription = null;
          await reconcileActivity(request, swarmId, participant, {
            settled: true,
            background,
          });
        }
        return;
      }
      key = subscription.subscription_id;
      replayThrough = subscription.replay_through_sequence;
      replayCaughtUp = replayThrough === 0;
      if (host.model.disposed || request !== activityRequest) {
        off();
        unsubscribe(key);
        return;
      }
      for (const [id, event] of buffered) receive(id, event);
    } catch (cause) {
      off();
      if (!host.model.disposed && request === activityRequest) {
        currentSubscription = null;
        readFailed(cause, background);
      }
    }
  }
  function destroy() {
    activityRequest += 1;
    clearProjection();
    currentSubscription?.();
  }

  async function cancelToolCall({ runId, toolCallId }) {
    const groupId = host.model.selectedSwarm?.id;
    if (!groupId || !history || runId !== history.participant.lifecycle_run_id)
      return;
    const request = activityRequest;
    try {
      await host.model.client.cancelToolCall(groupId, runId, toolCallId);
    } catch (cause) {
      if (!host.model.disposed && request === activityRequest)
        host.model.error = failureText(cause);
    }
  }

  return {
    cancelToolCall,
    isReasoningOpen,
    setReasoningOpen,
    destroy,
    get history() {
      return history;
    },
    get historyLoading() {
      return historyLoading;
    },
    leaveActivity,
    get activityTimeline() {
      return activityTimeline;
    },
    get selectedParticipant() {
      return selectedParticipant;
    },
    get contextWindow() {
      return contextWindow;
    },
    get stopReason() {
      return stopReason;
    },
    loadEarlierActivity,
    inspectParticipant,
  };
}
