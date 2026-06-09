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
    submittedTurnScrollKey = 0,
    submittedTurnScrollRunId = '',
    subAgentStatuses = {},
    subAgentResults = {},
    onNavigateToSubAgent = () => {},
    onRequestSubAgentResult = () => {},
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

  let timelineItems = $derived(visibleTimelineItemsForRender(sessionState));
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
    timelineItems.map((item) => timelineItemSignature(item)).join('|'),
  );
  let shouldRenderSubmittedTurnScrollSpacer = $derived(
    hasSubmittedTurnUserItem(),
  );

  $effect(() => {
    if (
      submittedTurnScrollKey > handledSubmittedTurnScrollKey &&
      submittedTurnScrollKey > pendingSubmittedTurnScrollKey
    ) {
      pendingSubmittedTurnScrollKey = submittedTurnScrollKey;
      pendingSubmittedTurnScrollRunId = submittedTurnScrollRunId;
      syncSubmittedTurnSpacerHeight();
    }
  });

  $effect.pre(() => {
    timelineSignature;
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
  $effect(() => {
    const container = scrollContainer;
    if (!container) {
      return undefined;
    }
    container.addEventListener('click', handleTimelineClick);
    return () => container.removeEventListener('click', handleTimelineClick);
  });

  function isNearBottom(container) {
    return (
      !container ||
      container.offsetHeight + container.scrollTop > container.scrollHeight - 56
    );
  }

  function timelineItemSignature(item) {
    if (item.type === 'streaming') {
      if (item.streamingItem?.type === 'tool_call') {
        return `${item.id}:${item.streamingItem.sequence}:${(item.streamingItem.name ?? '').length}:${(item.streamingItem.argumentsText ?? '').length}`;
      }
      return `${item.id}:${item.streamingItem.sequence}`;
    }
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

  function handleTimelineClick(event) {
    const image = event.target;
    if (!(image instanceof HTMLImageElement)) {
      return;
    }
    // Only rendered Markdown images open the lightbox; user attachment
    // thumbnails keep their existing open-in-new-tab link behavior.
    if (!image.closest('.msg-markdown')) {
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
    {#if timelineItems.length === 0}
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
            {onRetry}
            {onCancelToolCall}
            {onCancelSubAgent}
            showRetry={shouldRenderRetryButton(item, latestTerminalState)}
          />
        {:else}
          <ChatTimelineEntry
            {item}
            {timelineItems}
            {agentName}
            {isReasoningOpen}
            onReasoningOpenChange={setReasoningOpen}
            {onRetry}
            showRetry={shouldRenderRetryButton(item, latestTerminalState)}
          />
        {/if}
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

{#if lightboxImage}
  <ImageLightbox
    src={lightboxImage.src}
    alt={lightboxImage.alt}
    onClose={closeLightbox}
  />
{/if}
