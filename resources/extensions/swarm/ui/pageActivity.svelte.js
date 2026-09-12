import { visibleTimelineItemsForRender } from '../../../../webui/src/lib/chatTimeline.js';
import { t, activeLocaleTag } from '../../../../webui/src/lib/i18n.js';
import { formatTokenUsageTooltip } from '../../../../webui/src/lib/tokenUsageTooltip.js';
import { SvelteSet } from 'svelte/reactivity';

export function createSwarmPageActivity(host) {
  let history = $state(null);

  let live = $state([]);

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
    live = [];
  }

  const activityTimeline = $derived(
    history
      ? visibleTimelineItemsForRender({
          messages: history.data.messages ?? [],
          runEvents: live,
          streamingRunEvents: [],
          status:
            history.data.status ??
            (history.participant.run_active ? 'running' : 'completed'),
          currentRun: { runId: history.participant.lifecycle_run_id },
        })
      : [],
  );

  const selectedParticipant = $derived(
    host.model.selectedSwarm?.participants?.find(
      (item) => item.id === history?.participant.id,
    ),
  );

  const contextWindow = $derived(
    host.model.catalog.models?.find(
      (model) => model.id === history?.participant.model,
    )?.context_window,
  );

  const contextRatio = $derived(
    Number.isFinite(contextWindow) &&
      contextWindow > 0 &&
      Number.isFinite(history?.data.context_usage?.tokens)
      ? Math.min(1, history.data.context_usage.tokens / contextWindow)
      : 0,
  );

  const contextTokens = $derived(
    Number.isFinite(history?.data.context_usage?.tokens)
      ? `${history.data.context_usage.estimated ? '~' : ''}${new Intl.NumberFormat(activeLocaleTag()).format(history.data.context_usage.tokens)}${contextWindow ? ` / ${new Intl.NumberFormat(activeLocaleTag()).format(contextWindow)}` : ''}`
      : t('swarm.usage.unavailable', 'Unavailable'),
  );

  const activityContextTooltip = $derived(
    formatTokenUsageTooltip(
      history?.data.context_usage,
      history?.data.usage,
      history?.data.session_usage,
      contextWindow,
    ),
  );

  async function reconcileActivity(
    request,
    swarmId,
    participant,
    settled = false,
  ) {
    const read = ++historyRequest;
    const pageCount = historyPageCount;
    historyLoading = true;
    try {
      const data = await host.model.client.readHistory(
        swarmId,
        participant.id,
        {
          limit: 100,
        },
      );
      const pages = [data];
      while (
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
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      ) {
        const oldest = pages.at(-1);
        historyPageCount = pages.length;
        history = {
          participant: settled
            ? { ...participant, run_active: false }
            : participant,
          data: {
            ...data,
            messages: pages.toReversed().flatMap((page) => page.messages ?? []),
            has_more: oldest.has_more,
            next_before: oldest.next_before,
          },
        };
        if (settled) live = [];
      }
    } catch (cause) {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        host.model.error = cause.message;
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
          has_more: data.has_more,
          next_before: data.next_before,
        },
      };
    } catch (cause) {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        host.model.error = cause.message;
    } finally {
      if (
        !host.model.disposed &&
        request === activityRequest &&
        read === historyRequest
      )
        historyLoading = false;
    }
  }

  async function inspectParticipant(
    participant,
    { activate = true, preserve = false } = {},
  ) {
    if (!host.model.selectedSwarm) return;
    if (activate) host.model.activeTab = 'participants';
    if (preserve) {
      activityRequest += 1;
      currentSubscription?.();
      currentSubscription = null;
    } else leaveActivity();
    const request = activityRequest;
    const swarmId = host.model.selectedSwarm.id;
    await Promise.all([
      reconcileActivity(
        request,
        swarmId,
        participant,
        participant.run_active === false,
      ),
      host.model.catalog.models
        ? Promise.resolve()
        : host.model
            .call('catalog')
            .then((result) => {
              host.model.catalog = result.catalog;
            })
            .catch((cause) => {
              if (request === activityRequest) host.model.error = cause.message;
            }),
    ]);
    if (
      host.model.disposed ||
      request !== activityRequest ||
      !history ||
      !participant.lifecycle_run_id ||
      participant.run_active === false
    )
      return;
    let key = null;
    const buffered = [];
    const sequences = new SvelteSet();
    live = [];
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
      live = [...live, event];
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
      if (
        [
          'run_completed',
          'run_cancelled',
          'run_failed',
          'run_interrupted',
        ].includes(event.type)
      ) {
        currentSubscription?.();
        currentSubscription = null;
        void reconcileActivity(request, swarmId, participant, true);
      }
    };
    const off = host.model.client.onRunEvent((id, event) => {
      if (key === null) buffered.push([id, event]);
      else receive(id, event);
    });
    const unsubscribe = (id) => {
      void host.model.client.unsubscribeRun(id).catch((cause) => {
        if (!host.model.disposed && request === activityRequest)
          host.model.error = cause.message;
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
          await reconcileActivity(request, swarmId, participant, true);
        }
        return;
      }
      key = subscription.subscription_id;
      if (host.model.disposed || request !== activityRequest) {
        off();
        unsubscribe(key);
        return;
      }
      for (const [id, event] of buffered) receive(id, event);
    } catch (cause) {
      off();
      if (request === activityRequest) host.model.error = cause.message;
    }
  }
  function destroy() {
    activityRequest += 1;
    currentSubscription?.();
  }

  return {
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
    get contextRatio() {
      return contextRatio;
    },
    get contextTokens() {
      return contextTokens;
    },
    get activityContextTooltip() {
      return activityContextTooltip;
    },
    loadEarlierActivity,
    inspectParticipant,
  };
}
