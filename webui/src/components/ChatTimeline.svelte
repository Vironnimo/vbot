<script>
  import { tick } from 'svelte';

  import {
    dateKeyForTimestamp,
    formatDate,
    latestTerminalStateForItems,
    shouldRenderRetryButton,
    timestampForItem,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';

  import {
    assistantRunChildProgressKey,
    visibleTimelineItemsForRender,
  } from '../lib/chatState.js';
  import ChatAssistantRun from './chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from './chat/ChatTimelineEntry.svelte';
  import ImageLightbox from './ImageLightbox.svelte';

  let {
    sessionState,
    agentName = '',
    transientCards = [],
    submittedTurnScrollKey = 0,
    submittedTurnScrollRunId = '',
    subAgentStatuses = {},
    subAgentResults = {},
    onNavigateToSubAgent = () => {},
    onRequestSubAgentResult = () => {},
    onVerifySubAgentStatus = () => {},
    onRetry = () => {},
    onCancelToolCall = () => {},
    onCancelSubAgent = () => {},
    hasOlderHistory = false,
    loadingOlderHistory = false,
    onLoadOlder = async () => false,
  } = $props();

  const SUBMITTED_TURN_SCROLL_OPTIONS = Object.freeze({
    block: 'start',
    inline: 'nearest',
    behavior: 'smooth',
  });
  const MIN_SUBMITTED_TURN_SPACER_HEIGHT = 360;
  const LOAD_OLDER_SCROLL_THRESHOLD = 48;
  const SESSION_SCROLL_POSITION_LIMIT = 100;

  let timelineItems = $derived(visibleTimelineItemsForRender(sessionState));
  // Transient cards interleaved with the timeline: each renders after the
  // item it was anchored to (`leading` for cards created on an empty timeline,
  // `trailing` for cards whose anchor item is gone after a history reload).
  let transientCardGroups = $derived(
    groupTransientCards(timelineItems, transientCards),
  );
  let timelineDateKeys = $derived(
    timelineItems.map((item) => dateKeyForTimestamp(timestampForItem(item))),
  );
  let shouldShowTimelineDateSeparators = $derived(
    new Set(timelineDateKeys.filter(Boolean)).size > 1,
  );
  let scrollContainer = $state();
  let lightboxImage = $state(null);
  let reasoningDisclosureState = $state({});
  let pendingSubmittedTurnScrollKey = $state(0);
  let pendingSubmittedTurnScrollRunId = $state('');
  let handledSubmittedTurnScrollKey = $state(0);
  let loadingOlderFromScroll = $state(false);
  let submittedTurnSpacerHeight = $state(MIN_SUBMITTED_TURN_SPACER_HEIGHT);
  let latestTerminalState = $derived(
    latestTerminalStateForItems(timelineItems),
  );
  let timelineSignature = $derived(
    `${timelineItems.map((item) => timelineItemSignature(item)).join('|')}` +
      `#${transientCards.map((card) => card.id).join(',')}`,
  );
  let shouldRenderSubmittedTurnScrollSpacer = $derived(
    hasSubmittedTurnUserItem(),
  );
  let sessionScrollKey = $derived(sessionState?.key ?? '');
  // Per-session scroll memory: the scroll container survives session
  // switches (sub-agent "View session", drawer picks, "Return to current
  // session"), so each displayed session's position is saved on switch-away
  // and restored on switch-back. Deliberately plain (non-reactive)
  // bookkeeping: the map is read and written inside the scroll pre-effect,
  // and a SvelteMap would register itself as a dependency of that effect.
  let renderedSessionScrollKey = null;
  // While a mid-history restore is pinned, content turbulence after a session
  // switch (history reload, late-rendering content, run events) re-asserts the
  // restored position instead of letting stick-to-bottom steal it; only real
  // user scroll input (wheel/touch/pointer/keys) or a new submitted turn
  // releases the pin.
  let pinnedRestoreTop = null;
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  const sessionScrollPositions = new Map();

  $effect(() => {
    if (
      submittedTurnScrollKey > handledSubmittedTurnScrollKey &&
      submittedTurnScrollKey > pendingSubmittedTurnScrollKey
    ) {
      pendingSubmittedTurnScrollKey = submittedTurnScrollKey;
      pendingSubmittedTurnScrollRunId = submittedTurnScrollRunId;
      pinnedRestoreTop = null;
      syncSubmittedTurnSpacerHeight();
    }
  });

  // One pre-effect owns all scroll behaviors so they cannot fight: a
  // session switch saves the outgoing session's position (in a pre-effect
  // the DOM still shows the old session) and restores the incoming one's
  // after render; while a mid-history restore is pinned, content changes
  // re-assert it; otherwise content changes within the same session keep
  // the stick-to-bottom behavior.
  $effect.pre(() => {
    timelineSignature;
    const key = sessionScrollKey;
    if (key !== renderedSessionScrollKey) {
      saveSessionScrollPosition(renderedSessionScrollKey);
      renderedSessionScrollKey = key;
      pinnedRestoreTop = null;
      tick().then(() => restoreSessionScrollPosition(key));
      return;
    }
    if (pinnedRestoreTop !== null) {
      const top = pinnedRestoreTop;
      tick().then(() => {
        if (pinnedRestoreTop === top && key === renderedSessionScrollKey) {
          scrollContainer?.scrollTo?.(0, top);
        }
      });
      return;
    }
    const shouldAutoscroll =
      !hasPendingSubmittedTurnScroll() && isNearBottom(scrollContainer);
    if (shouldAutoscroll) {
      tick().then(() => {
        scrollContainer?.scrollTo?.(0, scrollContainer.scrollHeight);
      });
    }
  });

  $effect(() => {
    timelineSignature;
    if (!hasPendingSubmittedTurnScroll()) {
      return;
    }

    tick().then(async () => {
      if (!hasPendingSubmittedTurnScroll()) {
        return;
      }
      const target = submittedTurnScrollTarget(userMessageElements());
      if (!target) {
        return;
      }
      syncSubmittedTurnSpacerHeight(target);
      await tick();
      if (!hasPendingSubmittedTurnScroll()) {
        return;
      }
      if (scrollSubmittedTurnIntoView()) {
        handledSubmittedTurnScrollKey = pendingSubmittedTurnScrollKey;
      }
    });
  });

  $effect(() => {
    timelineSignature;
    if (!shouldRenderSubmittedTurnScrollSpacer) {
      return;
    }

    tick().then(() => {
      const target = submittedTurnScrollTarget(userMessageElements());
      if (target) {
        syncSubmittedTurnSpacerHeight(target);
      }
    });
  });

  // Delegated listener (not a markup handler) because Markdown images are
  // rendered through {@html} and cannot carry their own Svelte click handler.
  // The user-input listeners release a pinned restore position: only the
  // user moving the view (not programmatic scrolls or browser re-clamps)
  // hands scroll ownership back to the stick-to-bottom behavior.
  $effect(() => {
    const container = scrollContainer;
    if (!container) {
      return undefined;
    }
    const releasePinnedRestore = () => {
      pinnedRestoreTop = null;
    };
    container.addEventListener('click', handleTimelineClick);
    container.addEventListener('wheel', releasePinnedRestore, {
      passive: true,
    });
    container.addEventListener('touchstart', releasePinnedRestore, {
      passive: true,
    });
    container.addEventListener('pointerdown', releasePinnedRestore);
    container.addEventListener('keydown', releasePinnedRestore);
    return () => {
      container.removeEventListener('click', handleTimelineClick);
      container.removeEventListener('wheel', releasePinnedRestore);
      container.removeEventListener('touchstart', releasePinnedRestore);
      container.removeEventListener('pointerdown', releasePinnedRestore);
      container.removeEventListener('keydown', releasePinnedRestore);
    };
  });

  function isNearBottom(container) {
    return (
      !container ||
      container.offsetHeight + container.scrollTop > container.scrollHeight - 56
    );
  }

  function timelineItemSignature(item) {
    if (item.type === 'assistant_run') {
      return `${item.id}:${item.status}:${(item.items ?? [])
        .map(
          (child) =>
            `${child.id}:${child.type}:${child.sequence ?? ''}:${child.status ?? ''}:${child.streaming ? '1' : '0'}:${assistantRunChildProgressKey(child)}`,
        )
        .join('~')}`;
    }
    return item.id;
  }

  function groupTransientCards(items, cards) {
    const itemIds = new Set(items.map((item) => item.id));
    const groups = { leading: [], byItemId: new Map(), trailing: [] };
    for (const card of cards) {
      if (card.anchorId && itemIds.has(card.anchorId)) {
        const anchored = groups.byItemId.get(card.anchorId) ?? [];
        anchored.push(card);
        groups.byItemId.set(card.anchorId, anchored);
      } else if (card.anchorId == null) {
        groups.leading.push(card);
      } else {
        // The anchor item no longer exists (e.g. a history reload changed ids);
        // keep the card visible at the end rather than dropping it.
        groups.trailing.push(card);
      }
    }
    return groups;
  }

  function handleTimelineClick(event) {
    const image = event.target;
    if (!(image instanceof HTMLImageElement)) {
      return;
    }
    // Both rendered Markdown images and user attachment thumbnails open the
    // lightbox. Modifier clicks fall through so the attachment link can still
    // open the raw image in a new tab.
    if (
      !image.closest('.msg-markdown') &&
      !image.closest('.inline-attachment')
    ) {
      return;
    }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    lightboxImage = { src: image.currentSrc || image.src, alt: image.alt };
  }

  function closeLightbox() {
    lightboxImage = null;
  }

  function isReasoningOpen(id) {
    return Boolean(reasoningDisclosureState[id]);
  }

  function setReasoningOpen(id, isOpen) {
    reasoningDisclosureState[id] = isOpen;
  }

  function shouldRenderTimelineDateSeparator(itemIndex) {
    if (!shouldShowTimelineDateSeparators) {
      return false;
    }

    const currentDateKey = timelineDateKeys[itemIndex];
    return Boolean(
      currentDateKey && currentDateKey !== timelineDateKeys[itemIndex - 1],
    );
  }

  function hasPendingSubmittedTurnScroll() {
    return pendingSubmittedTurnScrollKey > handledSubmittedTurnScrollKey;
  }

  function saveSessionScrollPosition(key) {
    if (!key || !scrollContainer) {
      return;
    }
    sessionScrollPositions.delete(key);
    sessionScrollPositions.set(key, {
      top: scrollContainer.scrollTop,
      atBottom: isNearBottom(scrollContainer),
    });
    while (sessionScrollPositions.size > SESSION_SCROLL_POSITION_LIMIT) {
      const oldestKey = sessionScrollPositions.keys().next().value;
      sessionScrollPositions.delete(oldestKey);
    }
  }

  function restoreSessionScrollPosition(key) {
    if (key !== renderedSessionScrollKey) {
      // A newer switch superseded this restore; its own restore handles it.
      return;
    }
    if (!key || !scrollContainer || hasPendingSubmittedTurnScroll()) {
      return;
    }
    const saved = sessionScrollPositions.get(key);
    if (saved && !saved.atBottom) {
      pinnedRestoreTop = saved.top;
      scrollContainer.scrollTo?.(0, saved.top);
      return;
    }
    // First view of this session, or the user left it at the bottom: start
    // at the newest content (content may have grown while away).
    scrollContainer.scrollTo?.(0, scrollContainer.scrollHeight);
  }

  function scrollSubmittedTurnIntoView() {
    const target = submittedTurnScrollTarget(userMessageElements());
    if (!target) {
      return false;
    }

    if (typeof target.scrollIntoView === 'function') {
      target.scrollIntoView(SUBMITTED_TURN_SCROLL_OPTIONS);
      return true;
    }

    scrollContainer?.scrollTo?.(0, target.offsetTop ?? 0);
    return true;
  }

  function userMessageElements() {
    return Array.from(scrollContainer?.querySelectorAll?.('.msg.user') ?? []);
  }

  function submittedTurnScrollTarget(userMessages) {
    if (pendingSubmittedTurnScrollRunId) {
      return (
        userMessages.find(
          (element) =>
            element.dataset.runId === pendingSubmittedTurnScrollRunId,
        ) ?? null
      );
    }
    return userMessages[userMessages.length - 1] ?? null;
  }

  async function handleMessagesScroll() {
    if (!shouldLoadOlderHistory()) {
      return;
    }

    const previousScrollHeight = scrollContainer.scrollHeight;
    const previousScrollTop = scrollContainer.scrollTop;
    loadingOlderFromScroll = true;
    try {
      const loaded = await onLoadOlder?.();
      if (loaded === false) {
        return;
      }
      await tick();
      const scrollHeightDelta =
        scrollContainer.scrollHeight - previousScrollHeight;
      scrollContainer.scrollTop = previousScrollTop + scrollHeightDelta;
    } finally {
      loadingOlderFromScroll = false;
    }
  }

  function shouldLoadOlderHistory() {
    return (
      hasOlderHistory &&
      !loadingOlderHistory &&
      !loadingOlderFromScroll &&
      timelineItems.length > 0 &&
      scrollContainer &&
      scrollContainer.scrollTop <= LOAD_OLDER_SCROLL_THRESHOLD
    );
  }

  function hasSubmittedTurnUserItem() {
    if (!pendingSubmittedTurnScrollKey) {
      return false;
    }
    if (!pendingSubmittedTurnScrollRunId) {
      return hasAnyUserTimelineItem();
    }
    return timelineItems.some(
      (item) =>
        userRunIdForTimelineItem(item) === pendingSubmittedTurnScrollRunId,
    );
  }

  function hasAnyUserTimelineItem() {
    return timelineItems.some(
      (item) => item.type === 'message' && item.message.role === 'user',
    );
  }

  function userRunIdForTimelineItem(item) {
    if (item?.type === 'message' && item.message.role === 'user') {
      return item.message.run_id ?? '';
    }
    if (
      item?.type === 'event' &&
      item.event.type === 'user_message_persisted'
    ) {
      return item.event.run_id ?? '';
    }
    return '';
  }

  function syncSubmittedTurnSpacerHeight(target = null) {
    const containerHeight = scrollContainer?.clientHeight ?? 0;
    if (!scrollContainer || containerHeight <= 0 || !target) {
      submittedTurnSpacerHeight = Math.max(
        containerHeight,
        MIN_SUBMITTED_TURN_SPACER_HEIGHT,
      );
      return;
    }

    const spacer = scrollContainer.querySelector(
      '.submitted-turn-scroll-spacer',
    );
    const currentSpacerHeight = spacer?.getBoundingClientRect().height ?? 0;
    const containerRect = scrollContainer.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const targetTop =
      targetRect.top - containerRect.top + scrollContainer.scrollTop;
    const contentHeightWithoutSpacer =
      scrollContainer.scrollHeight - currentSpacerHeight;
    submittedTurnSpacerHeight = Math.max(
      0,
      Math.ceil(targetTop + containerHeight - contentHeightWithoutSpacer),
    );
  }
</script>

<section
  class="messages"
  bind:this={scrollContainer}
  aria-live="polite"
  onscroll={handleMessagesScroll}
>
  <div class="messages__content">
    {#if timelineItems.length === 0 && transientCards.length === 0}
      <div class="empty-state chat-empty-state">
        <svg class="empty-state-icon" viewBox="0 0 32 32" aria-hidden="true">
          <path d="M5 7h22v14H16l-6 5v-5H5z" />
        </svg>
        <p class="empty-state-title">
          {t('chat.historyEmptyTitle', 'No messages yet')}
        </p>
        <p class="empty-state-sub">
          {t(
            'chat.historyEmpty',
            'No messages yet. Send the first message to this agent.',
          )}
        </p>
      </div>
    {:else}
      {#each transientCardGroups.leading as card (card.id)}
        {@render transientCard(card)}
      {/each}
      {#each timelineItems as item, itemIndex (item.id)}
        {#if shouldRenderTimelineDateSeparator(itemIndex)}
          <div class="date-sep">
            {formatDate(timestampForItem(item))}
          </div>
        {/if}
        {#if item.type === 'assistant_run'}
          <ChatAssistantRun
            {item}
            {agentName}
            {subAgentStatuses}
            {subAgentResults}
            {isReasoningOpen}
            onReasoningOpenChange={setReasoningOpen}
            {onNavigateToSubAgent}
            {onRequestSubAgentResult}
            {onVerifySubAgentStatus}
            {onRetry}
            {onCancelToolCall}
            {onCancelSubAgent}
            showRetry={shouldRenderRetryButton(item, latestTerminalState)}
          />
        {:else}
          <ChatTimelineEntry
            {item}
            {agentName}
            {isReasoningOpen}
            onReasoningOpenChange={setReasoningOpen}
            {onRetry}
            showRetry={shouldRenderRetryButton(item, latestTerminalState)}
          />
        {/if}
        {#each transientCardGroups.byItemId.get(item.id) ?? [] as card (card.id)}
          {@render transientCard(card)}
        {/each}
      {/each}
      {#each transientCardGroups.trailing as card (card.id)}
        {@render transientCard(card)}
      {/each}
      {#if shouldRenderSubmittedTurnScrollSpacer}
        <div
          class="submitted-turn-scroll-spacer"
          style={`height: ${submittedTurnSpacerHeight}px`}
          aria-hidden="true"
        ></div>
      {/if}
    {/if}
  </div>
</section>

{#snippet transientCard(card)}
  <div
    class="transient-card"
    role="note"
    aria-label={t('chat.transientCard.label', 'Command output')}
  >
    <span class="transient-card__label">
      {t('chat.transientCard.label', 'Command output')}
    </span>
    <pre class="transient-card__body">{card.text}</pre>
  </div>
{/snippet}

{#if lightboxImage}
  <ImageLightbox
    src={lightboxImage.src}
    alt={lightboxImage.alt}
    onClose={closeLightbox}
  />
{/if}
