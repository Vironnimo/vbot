<script>
  import { tick } from 'svelte';

  import {
    dateKeyForTimestamp,
    formatDate,
    liveClockCadenceMs,
    timestampForItem,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { createChatScrollController } from '$lib/chatScroll.js';

  import {
    assistantRunChildProgressKey,
    visibleTimelineItemsForRender,
  } from '../lib/chatState.js';
  import ChatAssistantRun from './chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from './chat/ChatTimelineEntry.svelte';
  import ImageLightbox from './ImageLightbox.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import CopyButton from './ui/CopyButton.svelte';
  import EmptyState from './ui/EmptyState.svelte';

  let {
    sessionState,
    agentName = '',
    chatWorkingMode = 'normal',
    transientCards = [],
    submittedTurnScrollKey = 0,
    submittedTurnScrollRunId = '',
    // Height of the floating composer stack overlaying this timeline's
    // bottom; the submitted-turn scroll alignment reserves space for it.
    bottomOverlayHeight = 0,
    // Explicit navigation through a Sub-Agent row starts that Session as a
    // live tail even when its ordinary per-Session viewport was saved higher
    // in History. Passive restoration does not send this request.
    followSessionRequest = null,
    subAgentStatuses = {},
    subAgentResults = {},
    onNavigateToSubAgent = () => {},
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

  const MIN_SUBMITTED_TURN_SPACER_HEIGHT = 360;

  let timelineItems = $derived(visibleTimelineItemsForRender(sessionState));
  let nowMs = $state(Date.now());
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
  let submittedTurnSpacerHeight = $state(MIN_SUBMITTED_TURN_SPACER_HEIGHT);
  let showJumpToLatest = $state(false);
  let timelineSignature = $derived(
    `${timelineItems.map((item) => timelineItemSignature(item)).join('|')}` +
      `#${transientCards.map((card) => card.id).join(',')}`,
  );
  let shouldRenderSubmittedTurnScrollSpacer = $derived(
    hasSubmittedTurnUserItem(),
  );
  let sessionScrollKey = $derived(sessionState?.key ?? '');
  let renderedSessionScrollKey = null;
  let handledFollowSessionRequestId = 0;
  // Owns follow/reading modes, per-Session viewports, programmatic writes,
  // and user-scroll classification. Created once the scroll container binds.
  let controller = null;

  $effect(() => {
    const visibleItems = timelineItems;
    const statuses = subAgentStatuses;
    if (liveClockCadenceMs(visibleItems, statuses, Date.now()) === 0) {
      return undefined;
    }

    let timeoutId = null;
    let disposed = false;
    const clearClock = () => {
      if (timeoutId !== null) {
        clearTimeout(timeoutId);
        timeoutId = null;
      }
    };
    const scheduleClock = () => {
      clearClock();
      if (disposed || document.hidden) {
        return;
      }
      const delay = liveClockCadenceMs(visibleItems, statuses, Date.now());
      if (delay === 0) {
        return;
      }
      timeoutId = setTimeout(() => {
        timeoutId = null;
        nowMs = Date.now();
        scheduleClock();
      }, delay);
    };
    const handleVisibilityChange = () => {
      if (document.hidden) {
        clearClock();
        return;
      }
      nowMs = Date.now();
      scheduleClock();
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    nowMs = Date.now();
    scheduleClock();
    return () => {
      disposed = true;
      clearClock();
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  });

  // Runs before every DOM update so a session switch saves the outgoing
  // viewport against the still-mounted content. The controller itself is
  // created here too, guaranteeing it exists before any consumer runs.
  $effect.pre(() => {
    const container = scrollContainer;
    if (!container) {
      return;
    }
    if (!controller) {
      controller = createChatScrollController(container, {
        onViewChanged: syncJumpToLatestVisibility,
        shouldLoadOlder: () => shouldLoadOlderHistory(),
        requestLoadOlder: () => onLoadOlder?.(),
      });
    }
    const key = sessionScrollKey;
    if (key !== renderedSessionScrollKey) {
      renderedSessionScrollKey = key;
      controller.sessionChanged(key);
    }
  });

  $effect(() => {
    return () => {
      controller?.destroy();
      controller = null;
    };
  });

  $effect(() => {
    const requestId = followSessionRequest?.requestId ?? 0;
    const targetSessionKey = followSessionRequest?.sessionKey ?? '';
    if (
      requestId <= handledFollowSessionRequestId ||
      !targetSessionKey ||
      targetSessionKey !== sessionScrollKey
    ) {
      return;
    }
    handledFollowSessionRequestId = requestId;
    // Overrides whatever passive restore was prepared for this session:
    // an explicit Sub-Agent visit always opens at the bottom, following.
    // Deferred so the read sees the controller even when this effect runs
    // ahead of its creation during mount.
    tick().then(() => {
      controller?.forceFollowOnNextRestore();
      controller?.contentChanged();
    });
  });

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

  // Content changes (streaming deltas, history pages, transient cards)
  // re-run the coordination. The controller coalesces through one animation
  // frame — the same frame boundary the browser scrolls on. Deferred so the
  // read sees the controller even when this effect runs ahead of its
  // creation during mount. Skipped while the submitted-turn scroll owns the
  // viewport so the animation is never fought mid-flight.
  $effect(() => {
    timelineSignature;
    if (hasPendingSubmittedTurnScroll()) {
      return;
    }
    tick().then(() => controller?.contentChanged());
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
  // through {@html}. Input listeners feed real-user signals to the scroll
  // controller: upward input releases the follow pin before the browser
  // scrolls, and any input cancels an in-flight programmatic animation.
  $effect(() => {
    const container = scrollContainer;
    if (!container) {
      return undefined;
    }
    let touchY = null;
    const handleWheel = (event) => {
      controller?.noteUserInput({ upward: event.deltaY < 0 });
    };
    const handleTouchStart = (event) => {
      controller?.noteUserInput();
      touchY = event.touches?.[0]?.clientY ?? null;
    };
    const handleTouchMove = (event) => {
      const nextTouchY = event.touches?.[0]?.clientY ?? null;
      controller?.noteUserInput({
        upward: touchY !== null && nextTouchY !== null && nextTouchY > touchY,
      });
      touchY = nextTouchY;
    };
    const handleKeyDown = (event) => {
      controller?.noteUserInput({
        upward: isUpwardScrollKey(event.key),
      });
    };
    container.addEventListener('click', handleTimelineClick);
    container.addEventListener('wheel', handleWheel, {
      passive: true,
    });
    container.addEventListener('touchstart', handleTouchStart, {
      passive: true,
    });
    container.addEventListener('touchmove', handleTouchMove, {
      passive: true,
    });
    container.addEventListener('pointerdown', handlePointerDown);
    container.addEventListener('keydown', handleKeyDown);
    return () => {
      container.removeEventListener('click', handleTimelineClick);
      container.removeEventListener('wheel', handleWheel);
      container.removeEventListener('touchstart', handleTouchStart);
      container.removeEventListener('touchmove', handleTouchMove);
      container.removeEventListener('pointerdown', handlePointerDown);
      container.removeEventListener('keydown', handleKeyDown);
    };
  });

  function handlePointerDown() {
    controller?.noteUserInput();
  }

  // Growth invisible to Svelte state (images, fonts, Markdown layout) feeds
  // the same coordination path, coalesced by the controller.
  $effect(() => {
    const content = timelineContent;
    if (!content || typeof ResizeObserver !== 'function') {
      return undefined;
    }
    const observer = new ResizeObserver(() => {
      if (hasPendingSubmittedTurnScroll()) {
        return;
      }
      controller?.contentChanged();
    });
    observer.observe(content);
    return () => observer.disconnect();
  });

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

  function syncJumpToLatestVisibility() {
    showJumpToLatest = Boolean(
      sessionScrollKey &&
      scrollContainer &&
      controller &&
      !controller.isNearBottom(),
    );
  }

  function jumpToLatest() {
    if (!sessionScrollKey || !scrollContainer) {
      return;
    }
    controller?.pinToBottom();
  }

  function isUpwardScrollKey(key) {
    return key === 'ArrowUp' || key === 'PageUp' || key === 'Home';
  }

  function shouldLoadOlderHistory() {
    return (
      hasOlderHistory &&
      !loadingOlderHistory &&
      timelineItems.length > 0 &&
      Boolean(scrollContainer)
    );
  }

  function scrollSubmittedTurnIntoView() {
    const target = submittedTurnScrollTarget(userMessageElements());
    if (!target) {
      return false;
    }
    controller?.animateElementToTop(target);
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
    // The composer overlays the timeline's bottom; the aligned message must
    // stay visible above it, so only the unobstructed height counts.
    const containerHeight = Math.max(
      0,
      (scrollContainer?.clientHeight ?? 0) - bottomOverlayHeight,
    );
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

<div class="chat-timeline">
  <section class="messages" bind:this={scrollContainer} aria-live="polite">
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
                {chatWorkingMode}
                {subAgentStatuses}
                {subAgentResults}
                {nowMs}
                {isReasoningOpen}
                onReasoningOpenChange={setReasoningOpen}
                {onNavigateToSubAgent}
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
  {#if showJumpToLatest}
    <Button
      variant="secondary"
      icon
      class="chat-timeline__jump-latest"
      ariaLabel={t('chat.jumpToLatest', 'Jump to latest')}
      tooltip={t('chat.jumpToLatest', 'Jump to latest')}
      onClick={jumpToLatest}
    >
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path d="m6 9 6 6 6-6M12 4v11" />
      </svg>
    </Button>
  {/if}
</div>

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
