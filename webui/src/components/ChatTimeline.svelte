<script>
  import { tick } from 'svelte';

  import {
    dateKeyForTimestamp,
    formatDate,
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
  import Banner from './ui/Banner.svelte';
  import CopyButton from './ui/CopyButton.svelte';
  import EmptyState from './ui/EmptyState.svelte';

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
    onCancelToolCall = () => {},
    onCancelSubAgent = () => {},
    hasOlderHistory = false,
    loadingOlderHistory = false,
    // True while the displayed session's initial history request is in
    // flight; swaps the "No messages yet" empty state for a loading
    // placeholder so stepping through sessions does not flash it.
    loadingHistory = false,
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
  const FOLLOW_BOTTOM_THRESHOLD = 56;
  const USER_SCROLL_INTENT_WINDOW_MS = 750;

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
  let timelineContent = $state();
  let lightboxImage = $state(null);
  let reasoningDisclosureState = $state({});
  let pendingSubmittedTurnScrollKey = $state(0);
  let pendingSubmittedTurnScrollRunId = $state('');
  let handledSubmittedTurnScrollKey = $state(0);
  let loadingOlderFromScroll = $state(false);
  let submittedTurnSpacerHeight = $state(MIN_SUBMITTED_TURN_SPACER_HEIGHT);
  let timelineSignature = $derived(
    `${timelineItems.map((item) => timelineItemSignature(item)).join('|')}` +
      `#${transientCards.map((card) => card.id).join(',')}`,
  );
  let shouldRenderSubmittedTurnScrollSpacer = $derived(
    hasSubmittedTurnUserItem(),
  );
  let sessionScrollKey = $derived(sessionState?.key ?? '');
  let renderedSessionScrollKey = null;
  let viewportGeneration = 0;
  let pendingViewportSync = null;
  let viewportSyncQueued = false;
  let viewportSyncFrame = null;
  let userScrollIntentUntil = 0;
  // eslint-disable-next-line svelte/prefer-svelte-reactivity
  const sessionViewports = new Map();

  $effect(() => {
    if (
      submittedTurnScrollKey > handledSubmittedTurnScrollKey &&
      submittedTurnScrollKey > pendingSubmittedTurnScrollKey
    ) {
      pendingSubmittedTurnScrollKey = submittedTurnScrollKey;
      pendingSubmittedTurnScrollRunId = submittedTurnScrollRunId;
      setViewportMode(sessionScrollKey, 'follow');
      syncSubmittedTurnSpacerHeight();
    }
  });

  // Capture the outgoing DOM before Svelte replaces it, then let the single
  // viewport coordinator restore the incoming Session after render. Ordinary
  // content changes use the same coordinator; ResizeObserver below also feeds
  // it for growth that is invisible to Svelte state (images, fonts, Markdown).
  $effect.pre(() => {
    timelineSignature;
    const key = sessionScrollKey;
    if (key !== renderedSessionScrollKey) {
      saveSessionViewport(renderedSessionScrollKey);
      renderedSessionScrollKey = key;
      viewportGeneration += 1;
      beginViewportRestore(key);
      const generation = viewportGeneration;
      tick().then(() => queueViewportSync(key, generation));
      return;
    }
    const generation = viewportGeneration;
    tick().then(() => queueViewportSync(key, generation));
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

  // Delegated click handling is needed because Markdown images are rendered
  // through {@html}. Input listeners mark the next scroll as user-owned;
  // programmatic scroll events therefore cannot silently change follow mode.
  $effect(() => {
    const container = scrollContainer;
    if (!container) {
      return undefined;
    }
    const markUserScrollIntent = () => {
      userScrollIntentUntil = Date.now() + USER_SCROLL_INTENT_WINDOW_MS;
    };
    container.addEventListener('click', handleTimelineClick);
    container.addEventListener('wheel', markUserScrollIntent, {
      passive: true,
    });
    container.addEventListener('touchstart', markUserScrollIntent, {
      passive: true,
    });
    container.addEventListener('pointerdown', markUserScrollIntent);
    container.addEventListener('keydown', markUserScrollIntent);
    return () => {
      container.removeEventListener('click', handleTimelineClick);
      container.removeEventListener('wheel', markUserScrollIntent);
      container.removeEventListener('touchstart', markUserScrollIntent);
      container.removeEventListener('pointerdown', markUserScrollIntent);
      container.removeEventListener('keydown', markUserScrollIntent);
    };
  });

  $effect(() => {
    const content = timelineContent;
    if (!content) {
      return undefined;
    }
    const key = sessionScrollKey;
    const generation = viewportGeneration;
    const observer =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(() => queueViewportSync(key, generation))
        : null;
    observer?.observe(content);
    return () => observer?.disconnect();
  });

  $effect(() => {
    return () => {
      viewportGeneration += 1;
      cancelQueuedViewportSync();
    };
  });

  function isNearBottom(container) {
    return (
      !container ||
      container.offsetHeight + container.scrollTop >
        container.scrollHeight - FOLLOW_BOTTOM_THRESHOLD
    );
  }

  function createSessionViewport() {
    return {
      mode: 'follow',
      restoreMode: 'follow',
      anchorId: '',
      anchorOffset: 0,
      fallbackTop: 0,
    };
  }

  function sessionViewport(key) {
    if (!key) {
      return createSessionViewport();
    }
    let viewport = sessionViewports.get(key);
    if (!viewport) {
      viewport = createSessionViewport();
      sessionViewports.set(key, viewport);
      trimSessionViewports();
    }
    return viewport;
  }

  function trimSessionViewports() {
    while (sessionViewports.size > SESSION_SCROLL_POSITION_LIMIT) {
      const oldestKey = sessionViewports.keys().next().value;
      sessionViewports.delete(oldestKey);
    }
  }

  function setViewportMode(key, mode) {
    if (!key) {
      return;
    }
    const viewport = sessionViewport(key);
    viewport.mode = mode;
    viewport.restoreMode = mode;
    if (mode === 'follow') {
      viewport.anchorId = '';
    }
  }

  function beginViewportRestore(key) {
    const viewport = sessionViewport(key);
    viewport.restoreMode = viewport.mode === 'reading' ? 'reading' : 'follow';
    viewport.mode = 'restoring';
  }

  function queueViewportSync(
    key = sessionScrollKey,
    generation = viewportGeneration,
  ) {
    pendingViewportSync = { key, generation };
    if (viewportSyncQueued) {
      return;
    }
    viewportSyncQueued = true;
    if (typeof requestAnimationFrame === 'function') {
      viewportSyncFrame = requestAnimationFrame(flushViewportSync);
      return;
    }
    queueMicrotask(flushViewportSync);
  }

  function flushViewportSync() {
    viewportSyncQueued = false;
    viewportSyncFrame = null;
    const pending = pendingViewportSync;
    pendingViewportSync = null;
    if (
      !pending ||
      pending.key !== renderedSessionScrollKey ||
      pending.generation !== viewportGeneration ||
      !scrollContainer ||
      hasPendingSubmittedTurnScroll()
    ) {
      return;
    }
    applySessionViewport(pending.key);
  }

  function cancelQueuedViewportSync() {
    if (
      viewportSyncFrame !== null &&
      typeof cancelAnimationFrame === 'function'
    ) {
      cancelAnimationFrame(viewportSyncFrame);
    }
    viewportSyncFrame = null;
    viewportSyncQueued = false;
    pendingViewportSync = null;
  }

  function applySessionViewport(key) {
    const viewport = sessionViewport(key);
    const mode =
      viewport.mode === 'restoring' ? viewport.restoreMode : viewport.mode;
    if (mode === 'follow') {
      scrollContainer.scrollTop = scrollContainer.scrollHeight;
    } else {
      restoreViewportAnchor(viewport);
    }
    viewport.mode = mode;
  }

  function restoreViewportAnchor(viewport) {
    const anchor = timelineItemElement(viewport.anchorId);
    if (!anchor || !elementHasLayout(anchor)) {
      scrollContainer.scrollTop = viewport.fallbackTop;
      return;
    }
    const containerTop = scrollContainer.getBoundingClientRect().top;
    const currentOffset = anchor.getBoundingClientRect().top - containerTop;
    scrollContainer.scrollTop += currentOffset - viewport.anchorOffset;
  }

  function captureViewportAnchor(viewport) {
    if (!scrollContainer) {
      return;
    }
    viewport.fallbackTop = scrollContainer.scrollTop;
    const containerTop = scrollContainer.getBoundingClientRect().top;
    const anchor = timelineItemElements().find(
      (element) =>
        elementHasLayout(element) &&
        element.getBoundingClientRect().bottom > containerTop,
    );
    if (!anchor) {
      viewport.anchorId = '';
      viewport.anchorOffset = 0;
      return;
    }
    viewport.anchorId = anchor.dataset.timelineItemId ?? '';
    viewport.anchorOffset = anchor.getBoundingClientRect().top - containerTop;
  }

  function timelineItemElements() {
    return Array.from(
      timelineContent?.querySelectorAll?.('[data-timeline-item-id]') ?? [],
    );
  }

  function timelineItemElement(itemId) {
    if (!itemId) {
      return null;
    }
    return (
      timelineItemElements().find(
        (element) => element.dataset.timelineItemId === itemId,
      ) ?? null
    );
  }

  function elementHasLayout(element) {
    const rect = element.getBoundingClientRect();
    return rect.height > 0 || element.offsetHeight > 0;
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

  function saveSessionViewport(key) {
    if (!key || !scrollContainer) {
      return;
    }
    const viewport = sessionViewport(key);
    if (isNearBottom(scrollContainer)) {
      setViewportMode(key, 'follow');
    } else {
      viewport.mode = 'reading';
      viewport.restoreMode = 'reading';
      captureViewportAnchor(viewport);
    }
    sessionViewports.delete(key);
    sessionViewports.set(key, viewport);
    trimSessionViewports();
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

  function handleMessagesScroll() {
    if (Date.now() <= userScrollIntentUntil) {
      updateViewportFromUserScroll();
    }
    void loadOlderHistoryFromScroll();
  }

  function updateViewportFromUserScroll() {
    const viewport = sessionViewport(sessionScrollKey);
    if (isNearBottom(scrollContainer)) {
      setViewportMode(sessionScrollKey, 'follow');
      return;
    }
    viewport.mode = 'reading';
    viewport.restoreMode = 'reading';
    captureViewportAnchor(viewport);
  }

  async function loadOlderHistoryFromScroll() {
    if (!shouldLoadOlderHistory()) {
      return;
    }

    const key = sessionScrollKey;
    const generation = viewportGeneration;
    const viewport = sessionViewport(key);
    const previousScrollHeight = scrollContainer.scrollHeight;
    viewport.mode = 'reading';
    viewport.restoreMode = 'reading';
    captureViewportAnchor(viewport);
    viewport.mode = 'restoring';
    loadingOlderFromScroll = true;
    try {
      const loaded = await onLoadOlder?.();
      if (loaded === false) {
        return;
      }
      await tick();
      if (
        key === renderedSessionScrollKey &&
        generation === viewportGeneration
      ) {
        if (!viewport.anchorId) {
          viewport.fallbackTop +=
            scrollContainer.scrollHeight - previousScrollHeight;
        }
        applySessionViewport(key);
      }
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
  <div class="messages__content" bind:this={timelineContent}>
    {#if timelineItems.length === 0 && transientCards.length === 0}
      {#if loadingHistory}
        <!-- While history is loading, a quiet placeholder — flashing the
             "No messages yet" empty state would be a lie for a session whose
             messages just have not arrived yet. -->
        <Banner variant="neutral" class="chat-timeline-loading">
          {t('loading.history', 'Loading chat history…')}
        </Banner>
      {:else}
        <EmptyState
          fill
          class="chat-timeline-empty"
          title={t('chat.historyEmptyTitle', 'No messages yet')}
          description={t(
            'chat.historyEmpty',
            'No messages yet. Send the first message to this agent.',
          )}
        >
          {#snippet icon()}
            <svg viewBox="0 0 32 32" width="38" height="38">
              <path d="M5 7h22v14H16l-6 5v-5H5z" />
            </svg>
          {/snippet}
        </EmptyState>
      {/if}
    {:else}
      {#each transientCardGroups.leading as card (card.id)}
        {@render transientCard(card)}
      {/each}
      {#each timelineItems as item, itemIndex (item.id)}
        <div class="timeline-item" data-timeline-item-id={item.id}>
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
              {onCancelToolCall}
              {onCancelSubAgent}
            />
          {:else}
            <ChatTimelineEntry
              {item}
              {agentName}
              {isReasoningOpen}
              onReasoningOpenChange={setReasoningOpen}
            />
          {/if}
          {#each transientCardGroups.byItemId.get(item.id) ?? [] as card (card.id)}
            {@render transientCard(card)}
          {/each}
        </div>
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
    <div class="transient-card__header">
      <span class="transient-card__label">
        {t('chat.transientCard.label', 'Command output')}
      </span>
      <CopyButton
        text={card.text}
        class="chat-copy-action transient-card__copy"
        label={t('chat.copyCommandOutput', 'Copy command output')}
        copiedLabel={t('chat.commandOutputCopied', 'Command output copied')}
      />
    </div>
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
