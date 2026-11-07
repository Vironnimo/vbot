<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { t } from '../../../../webui/src/lib/i18n.js';
  import Banner from '../../../../webui/src/components/ui/Banner.svelte';
  import Button from '../../../../webui/src/components/ui/Button.svelte';
  import ChatAssistantRun from '../../../../webui/src/components/chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from '../../../../webui/src/components/chat/ChatTimelineEntry.svelte';
  import ContextUsageRing from '../../../../webui/src/components/chat/ContextUsageRing.svelte';
  import {
    createChatState,
    ensureSessionState,
    loadHistory,
    prependHistory,
  } from '../../../../webui/src/lib/chatState.js';
  import {
    applyTimelineEvent,
    visibleTimelineItemsForRender,
    mergeRunChildPage,
  } from '../../../../webui/src/lib/chatTimeline.js';
  import {
    participantId,
    participantName,
    failureText,
  } from './presentation.js';

  let { client, groupId, participant, onError, onClose } = $props();
  const projection = $state(
    ensureSessionState(createChatState(), 'factory', 'history'),
  );
  let history = $state(null);
  let loading = $state(false);
  let error = $state('');
  let reasoning = $state({});
  let visibility = $state(true);
  let mounted = $state(false);
  let request = 0;
  let read = 0;
  let disposed = false;
  let subscription = null;
  let offEvents = null;
  // Stream termination can precede the next owner invalidation.
  let settledRun = null;
  const timeline = $derived(visibleTimelineItemsForRender(projection));
  const gaps = $derived(
    timeline.flatMap((item) =>
      (item.items ?? [])
        .filter((child) => child.type === 'history_gap')
        .map((gap) => ({ runId: item.runId, before: gap.before })),
    ),
  );

  function historyOptions(data) {
    return {
      items: data.items,
      replaceRunIds: data.replace_run_ids,
      timelineSequence: data.timeline_sequence,
      activeRunId: data.active_run?.run_id,
      runs: data.runs,
      incremental: data.incremental,
      generation: data.history_generation,
      nextAfter: data.next_after,
      hasMore: data.has_more,
      nextBefore: data.next_before,
      sessionUsage: data.session_usage,
      contextUsage: data.context_usage,
    };
  }

  async function readHistory(current, before = null) {
    const token = ++read;
    loading = true;
    try {
      let after = before ? '' : projection.historyAfter;
      for (;;) {
        const data = await client.readHistory(
          groupId,
          participantId(participant),
          {
            limit: 100,
            child_limit: 100,
            ...(before ? { before } : after ? { after } : {}),
          },
        );
        if (disposed || current !== request || token !== read) return false;
        if (before)
          prependHistory(projection, data.messages ?? [], historyOptions(data));
        else loadHistory(projection, data.messages ?? [], historyOptions(data));
        history = data;
        if (
          before ||
          !data.incremental ||
          !data.has_newer ||
          !data.next_after ||
          data.next_after === after
        )
          break;
        after = data.next_after;
      }
      error = '';
      return true;
    } catch (cause) {
      if (!disposed && current === request && token === read)
        error = failureText(cause);
      return false;
    } finally {
      if (!disposed && current === request && token === read) loading = false;
    }
  }

  function stopStream() {
    offEvents?.();
    offEvents = null;
    const id = subscription;
    subscription = null;
    if (id)
      void client.unsubscribeRun(id).catch((cause) => {
        if (!disposed) onError(failureText(cause));
      });
  }

  async function refresh() {
    const current = ++request;
    stopStream();
    if (!(await readHistory(current)) || disposed || current !== request)
      return;
    const runId = participant.lifecycle_run_id;
    if (
      !participant.run_active ||
      !runId ||
      runId === settledRun ||
      !visibility
    )
      return;
    let key = null;
    const buffered = [];
    const receive = (id, event) => {
      if (
        disposed ||
        current !== request ||
        id !== key ||
        event.run_id !== runId
      )
        return;
      const applied = applyTimelineEvent(projection, event.type, event);
      if (
        !applied &&
        event.sequence > (projection.timelineSequences[runId] ?? -1)
      ) {
        void refresh();
        return;
      }
      if (event.state?.context_usage && history)
        history = { ...history, context_usage: event.state.context_usage };
      if (event.terminal) {
        settledRun = runId;
        stopStream();
        void readHistory(current);
      }
    };
    const off = client.onRunEvent((id, event) =>
      key === null ? buffered.push([id, event]) : receive(id, event),
    );
    offEvents = off;
    try {
      const result = await client.subscribeRun(groupId, runId, 0, {
        timeline: true,
        childLimit: 100,
      });
      key = result.subscription_id;
      if (disposed || current !== request) {
        off();
        if (key) await client.unsubscribeRun(key);
        return;
      }
      if (!key) {
        off();
        offEvents = null;
        await readHistory(current);
        return;
      }
      subscription = key;
      for (const [id, event] of buffered) receive(id, event);
    } catch (cause) {
      off();
      if (!disposed && current === request) {
        offEvents = null;
        error = failureText(cause);
      }
    }
  }

  $effect(() => {
    const identity = participant.lifecycle_run_id;
    const active = participant.run_active;
    if (!mounted) return;
    if (visibility)
      untrack(() => {
        void refresh();
      });
    else
      untrack(() => {
        request += 1;
        read += 1;
        loading = false;
        stopStream();
      });
    // Both lifecycle fields matter even when a new Run has the same activity.
    void identity;
    void active;
  });

  onMount(() => {
    const onVisibility = () => (visibility = !document.hidden);
    onVisibility();
    mounted = true;
    document.addEventListener('visibilitychange', onVisibility);
    const off = client.onInvalidation(({ change } = {}) => {
      if (visibility && (!change || change.ids.includes(groupId)))
        void refresh();
    });
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      off();
    };
  });
  onDestroy(() => {
    disposed = true;
    request += 1;
    read += 1;
    stopStream();
  });

  async function loadEarlierSteps(gap) {
    const current = request;
    const generation = projection.historyGeneration;
    loading = true;
    try {
      const data = await client.readRunChildren(
        groupId,
        participantId(participant),
        { ...gap, historyGeneration: generation, limit: 100 },
      );
      if (!disposed && current === request) mergeRunChildPage(projection, data);
    } catch (cause) {
      if (!disposed && current === request) error = failureText(cause);
    } finally {
      if (!disposed && current === request) loading = false;
    }
  }

  function loadToolCall({ runId, toolId }) {
    return client.readToolCall(groupId, participantId(participant), {
      runId,
      toolId,
      historyGeneration: projection.historyGeneration,
    });
  }

  async function cancelToolCall({ runId, toolCallId }) {
    try {
      await client.cancelToolCall(groupId, runId, toolCallId);
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  async function contentLink(event) {
    const anchor = event.target.closest?.('a[href]');
    if (!anchor) return;
    event.preventDefault();
    try {
      await client.openLink(anchor.getAttribute('href'));
    } catch (cause) {
      if (!disposed) error = failureText(cause);
    }
  }

  function contentLinks(node) {
    node.addEventListener('click', contentLink);
    return { destroy: () => node.removeEventListener('click', contentLink) };
  }
</script>

<section
  class="factory-section factory-session-history"
  aria-labelledby="factory-history-heading"
>
  <header class="factory-history-header">
    <div>
      <h3 id="factory-history-heading">
        {t('factory.history')} · {participantName(participant)}
      </h3>
      <span class="factory-help factory-mono"
        >{participant.address?.session_id}</span
      >
    </div>
    <ContextUsageRing
      contextUsage={history?.context_usage}
      contextWindow={history?.context_usage?.context_window}
      usage={history?.usage}
      sessionUsage={history?.session_usage}
      showFigures
    /><Button variant="tertiary" onClick={onClose}>{t('common.close')}</Button>
  </header>
  {#if error}<Banner variant="error" role="alert"
      >{error}<Button variant="tertiary" onClick={refresh}
        >{t('common.retry')}</Button
      ></Banner
    >{/if}
  {#if projection.hasOlderHistory && projection.historyBefore}<Button
      variant="secondary"
      disabled={loading}
      onClick={() => readHistory(request, projection.historyBefore)}
      >{t('factory.olderHistory')}</Button
    >{/if}
  {#each gaps as gap (`${gap.runId}:${gap.before}`)}<Button
      variant="tertiary"
      disabled={loading}
      onClick={() => loadEarlierSteps(gap)}>{t('factory.olderSteps')}</Button
    >{/each}
  <!-- Links in projected Chat content go through the host's verified grants. -->
  <div class="factory-history-timeline" use:contentLinks>
    {#each timeline as item (item.id)}
      {#if item.type === 'assistant_run'}<ChatAssistantRun
          {item}
          agentName={participantName(participant)}
          isReasoningOpen={(id) => Boolean(reasoning[id])}
          onReasoningOpenChange={(id, open) => (reasoning[id] = open)}
          onCancelToolCall={cancelToolCall}
          {loadToolCall}
        />{:else}<ChatTimelineEntry
          {item}
          agentName={participantName(participant)}
          messageEditingDisabled
        />{/if}
    {:else}{#if !loading}<p class="factory-help">
          {t('factory.noHistory')}
        </p>{/if}{/each}
  </div>
  {#if loading}<p class="factory-help" role="status">
      {t('common.loading')}
    </p>{/if}
</section>

<style>
  .factory-history-header {
    display: flex;
    align-items: center;
    gap: 16px;
    justify-content: space-between;
    margin-bottom: 20px;
  }
  .factory-history-header h3 {
    margin-bottom: 6px;
  }
  .factory-history-header > div:first-child {
    margin-right: auto;
    min-width: 0;
  }
  .factory-history-timeline {
    min-width: 0;
    display: flex;
    flex-direction: column;
  }
  .factory-session-history {
    container-type: inline-size;
  }
  .factory-history-timeline :global(.message) {
    max-width: 100%;
  }
  @media (max-width: 540px) {
    .factory-history-header {
      flex-wrap: wrap;
    }
  }
</style>
