<script>
  import { tick } from 'svelte';

  import {
    dateKeyForTimestamp,
    formatDate,
    groupTransientCards,
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
    // Height of the floating composer stack overlaying this timeline's
    // bottom; the bottom padding reserves space for it so the newest
    // content scrolls clear of the composer.
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
    messageEditingDisabled = false,
    onEditMessage = async () => false,
    hasOlderHistory = false,
    loadingOlderHistory = false,
    // True while the displayed session's initial history request is in
    // flight; swaps the "No messages yet" empty state for a loading
    // placeholder so stepping through sessions does not flash it.
    loadingHistory = false,
    onLoadOlder = async () => false,
    // Reports the scroller's real scrollbar width (0 with overlay
    // scrollbars) so ChatView can keep the floating composer stack clear of
    // the scrollbar column.
    onScrollbarWidthChange = () => {},
  } = $props();

  let timelineItems = $derived(visibleTimelineItemsForRender(sessionState));
  let nowMs = $state(Date.now());
  // Transient cards interleaved with the timeline: each renders after the
  // item it was anchored to; a card whose anchor is gone after a history
  // reload keeps its chronological position by creation time (see
  // `groupTransientCards`).
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
  let showJumpToLatest = $state(false);
  let timelineSignature = $derived(
    `${timelineItems.map((item) => timelineItemSignature(item)).join('|')}` +
      `#${transientCards.map((card) => card.id).join(',')}`,
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

  // A submitted turn pins the viewport to the bottom so the incoming
  // response is always visible. The controller follows the live tail as
  // content streams in.
  let lastSubmittedTurnScrollKey = 0;
  $effect(() => {
    if (submittedTurnScrollKey > lastSubmittedTurnScrollKey) {
      lastSubmittedTurnScrollKey = submittedTurnScrollKey;
      tick().then(() => controller?.pinToBottom());
    }
  });

  // Content changes (streaming deltas, history pages, transient cards)
  // re-run the coordination. The controller coalesces through one animation
  // frame — the same frame boundary the browser scrolls on. Deferred so the
  // read sees the controller even when this effect runs ahead of its
  // creation during mount.
  $effect(() => {
    timelineSignature;
    tick().then(() => controller?.contentChanged());
  });

  // Composer-stack growth (typing, attachment tray) only grows the reserved
  // bottom padding: content geometry is untouched, so the content
  // ResizeObserver stays silent and the coordination must be re-run
  // explicitly — a pinned session stays glued to the live tail while the
  // composer rises, a reading position needs no correction at all.
  $effect(() => {
    bottomOverlayHeight;
    tick().then(() => controller?.contentChanged());
  });

  // Delegated click handling is needed because Markdown images are rendered
  // through {@html}. Input listeners feed real-user signals to the scroll
  // controller: upward input releases the follow pin before the browser
  // scrolls, so concurrent content growth cannot yank the view back down.
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
      controller?.contentChanged();
    });
    observer.observe(content);
    return () => observer.disconnect();
  });

  // The scroller's real scrollbar width (0 with overlay scrollbars), so the
  // floating composer stack can end before the scrollbar column instead of
  // covering it. Observe the container itself because loading History can
  // introduce a classic scrollbar without resizing the window; that shrinks
  // the content box and must update the overlay inset immediately.
  $effect(() => {
    const container = scrollContainer;
    if (!container) {
      return undefined;
    }
    const reportWidth = () => {
      onScrollbarWidthChange(container.offsetWidth - container.clientWidth);
    };
    reportWidth();
    const observer =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(reportWidth)
        : null;
    observer?.observe(container);
    window.addEventListener('resize', reportWidth);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', reportWidth);
    };
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
                {messageEditingDisabled}
                {onEditMessage}
              />
            {/if}
            {#each transientCardGroups.byItemId.get(item.id) ?? [] as card (card.id)}
              {@render transientCard(card)}
            {/each}
            {#each transientCardGroups.byItemIndex.get(itemIndex) ?? [] as card (card.id)}
              {@render transientCard(card)}
            {/each}
          </div>
        {/each}
        {#each transientCardGroups.trailing as card (card.id)}
          {@render transientCard(card)}
        {/each}
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
