<script>
  import ContributionSurface from './extensions/ContributionSurface.svelte';
  import { tick, untrack } from 'svelte';

  import {
    conversationTimelineRows,
    dateKeyForTimestamp,
    formatDate,
    groupTransientCards,
    groupRunChildren,
    visibleRunChildren,
    userContentBlocks,
    labelForEvent,
    latestLiveClockStart,
    runFooterGroups,
    timestampForItem,
  } from '$lib/chatTimelinePresentation.js';
  import { t } from '$lib/i18n.js';
  import { createChatScrollController } from '$lib/chatScroll.js';
  import { timelineRowsForViewport } from '$lib/chatTimeline.js';
  import { locateSearchHit } from '$lib/chatState/sessionSearch.svelte.js';
  import { searchRange } from './chat/searchHighlight.svelte.js';
  import { nestedScrollConsumes } from '$lib/boundedScroll.js';
  import { provideMountHold } from '$lib/mountHold.js';

  import ChatAssistantRun from './chat/ChatAssistantRun.svelte';
  import ChatTimelineEntry from './chat/ChatTimelineEntry.svelte';
  import { createTimelineAnnouncer } from './chat/timelineAnnouncer.js';
  import {
    createTimelineViewState,
    provideTimelineViewState,
  } from './chat/timelineViewState.svelte.js';
  import MediaLightbox from './MediaLightbox.svelte';
  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ContextMenu from './ui/ContextMenu.svelte';
  import { contextMenuAnchor } from './ui/contextMenu.js';
  import CopyButton from './ui/CopyButton.svelte';
  import EmptyState from './ui/EmptyState.svelte';

  let {
    uiContext = {},
    // The displayed Session's rendered rows, projected once by the owner
    // through `visibleTimelineItemsForRender()`.
    timelineItems: sourceTimelineItems = [],
    // Key of the displayed Session: per-Session scroll memory and follow
    // requests.
    sessionKey = '',
    searchTarget = null,
    // The displayed Session's current Run; while it runs, its Tool rows offer
    // the server-advertised Move to background action.
    currentRun = null,
    agentName = '',
    chatWorkingMode = 'normal',
    presentation = 'full',
    readOnly = false,
    transientCards: sourceCards = [],
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
    backgroundCommandStatuses = {},
    commandStatuses = {},
    // The Session waits for work that will wake it (Session activity); its
    // latest Run's footer says so.
    sessionWaiting = false,
    // When the Session's current work span began while it is working or
    // waiting (Session activity), else null; its latest Run's footer counts
    // from there.
    sessionWorkStartedAt = null,
    onNavigateToSubAgent = () => {},
    // Open in split view for Sub-Agent links (`{ label, open(target) }`, with
    // the link's `{ agentId, sessionId }` target); null offers no link menu.
    otherArea = null,
    onCancelToolCall = null,
    onStopBackgroundCommand = null,
    onBackgroundToolCall = () => {},
    onCancelSubAgent = () => {},
    // Why User messages cannot be edited right now; '' allows editing.
    messageEditingDisabledReason = '',
    onEditMessage = async () => false,
    hasOlderHistory = false,
    loadingOlderHistory = false,
    // Whether to show initial History loading feedback; the owner may delay it.
    loadingHistory = false,
    // Only resolved History (or a draft with no Session) can be called empty,
    // independently of when loading feedback becomes visible.
    historyLoaded = true,
    onLoadOlder = async () => false,
    onLoadRunHistory = async () => false,
    // Reads the complete arguments and result of a Tool row: { runId, toolId }.
    onLoadToolCall = null,
    // Reports the scroller's real scrollbar width (0 with overlay
    // scrollbars) so ChatView can keep the floating composer stack clear of
    // the scrollbar column.
    onScrollbarWidthChange = () => {},
  } = $props();

  const conversation = $derived(presentation === 'conversation');
  const workingMode = $derived(
    presentation === 'compact' ? 'compact' : chatWorkingMode,
  );
  const presentedItems = $derived(
    conversation
      ? conversationTimelineRows(sourceTimelineItems)
      : sourceTimelineItems,
  );
  const transientCards = $derived(conversation ? [] : sourceCards);
  const timelineItems = $derived(
    timelineRowsForViewport(presentedItems, workingMode),
  );
  // Keyed by source item id: every viewport part of a Run carries its Run's.
  const latestRunItemId = $derived(
    sourceTimelineItems.findLast((item) => item.type === 'assistant_run')?.id ??
      null,
  );
  const footerGroups = $derived(
    runFooterGroups(sourceTimelineItems, {
      workStartedAt: sessionWorkStartedAt,
    }),
  );

  let nowMs = $state(Date.now());

  // The open Sub-Agent link menu (./ui/ContextMenu.svelte), or null.
  let subAgentMenu = $state(null);

  function openSubAgentMenu(event, target) {
    event.preventDefault();
    subAgentMenu = {
      ...contextMenuAnchor(event),
      label: t('chat.subagent.openSession'),
      items: [
        {
          id: 'open-in-other-area',
          label: otherArea.label,
          onSelect: () => otherArea.open(target),
        },
      ],
    };
  }
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
  // Disclosure and pending-action state of the rows, per displayed Session;
  // rows read it through context so it outlives their components.
  const viewState = provideTimelineViewState(
    createTimelineViewState(
      () => readOnly,
      () => searchTarget,
    ),
  );
  // Text of the polite live region: only genuinely new content (see
  // `timelineAnnouncer.js`), never rows the window mounts while scrolling.
  const announcer = createTimelineAnnouncer();
  let announcement = $state('');
  let announcedSessionKey = null;
  // Bumped whenever the scroll controller changes which rows to mount.
  let windowVersion = $state(0);
  let controllerReady = $state(false);
  // The rows and spacers to mount, decided by the scroll controller: every
  // row until the timeline has shown layout, then the rows in and around the
  // viewport plus held rows, with spacers standing in for the rest. Nothing
  // before the controller exists; it is created as soon as the scroller
  // binds, within the same update.
  let renderEntries = $derived.by(() => {
    windowVersion;
    const items = timelineItems;
    const key = sessionScrollKey;
    return controller ? controller.renderPlan(items, key) : [];
  });
  let itemIndexById = $derived(
    new Map(timelineItems.map((item, index) => [item.id, index])),
  );
  // The running Run's row stays mounted while the user reads elsewhere, so
  // its live output (a fresh speech result starting to play) does not depend
  // on the scroll position.
  let runningRowId = $derived(
    currentRun?.status === 'running'
      ? (timelineItems.findLast(
          (item) =>
            item.type === 'assistant_run' && item.runId === currentRun.runId,
        )?.id ?? '')
      : '',
  );
  let showJumpToLatest = $state(false);
  let transientCardIds = $derived(
    transientCards.map((card) => card.id).join(','),
  );
  let sessionScrollKey = $derived(sessionKey ?? '');
  let renderedSessionScrollKey = null;
  let handledFollowSessionRequestId = 0;
  // Owns follow/reading modes, per-Session viewports, programmatic writes,
  // and user-scroll classification. Created once the scroll container binds.
  let controller = null;

  const clockStart = $derived(
    latestLiveClockStart(
      presentedItems,
      subAgentStatuses,
      backgroundCommandStatuses,
      commandStatuses,
    ),
  );
  $effect(() => {
    const latestStart = clockStart;
    if (latestStart === null && !sessionWaiting) return;

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
      const delay =
        latestStart !== null && Date.now() - latestStart < 10_000 ? 100 : 1000;
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
    if (!container || !timelineContent) {
      return;
    }
    if (!controller) {
      controller = createChatScrollController(container, {
        content: timelineContent,
        onViewChanged: syncJumpToLatestVisibility,
        onWindowChanged: () => {
          windowVersion += 1;
        },
        shouldLoadOlder: () => shouldLoadOlderHistory(),
        requestLoadOlder: () => onLoadOlder?.(),
        requestLoadRunHistory: ({ runId, before }) =>
          onLoadRunHistory?.({ run_id: runId, before }),
      });
      controllerReady = true;
      windowVersion += 1;
    }
    const key = sessionScrollKey;
    if (key !== renderedSessionScrollKey) {
      renderedSessionScrollKey = key;
      controller.sessionChanged(key);
      viewState.setSession(key);
    }
  });

  $effect(() => {
    return () => {
      controller?.destroy();
      controller = null;
    };
  });

  // After every update of the mounted rows the controller measures new rows
  // and corrects the position before the browser paints.
  $effect(() => {
    renderEntries;
    untrack(() => controller?.rendered());
  });

  $effect(() => {
    const items = presentedItems;
    const cards = transientCards;
    const key = sessionScrollKey;
    const text = announcer.update(items, cards, key, {
      loading: loadingHistory,
    });
    untrack(() => {
      if (key !== announcedSessionKey) {
        announcedSessionKey = key;
        announcement = '';
      }
      if (text) {
        announcement = text;
      }
    });
  });

  $effect(() => {
    const id = runningRowId;
    if (!controllerReady || !id) {
      return undefined;
    }
    return untrack(() => controller?.hold(id));
  });

  // Row content with ongoing interaction (a playing player, an inline edit)
  // keeps its row mounted while it is scrolled out of the window.
  provideMountHold((element) => {
    const id = element?.closest?.('[data-timeline-item-id]')?.dataset
      .timelineItemId;
    return (id !== undefined && controller?.hold(id)) || (() => {});
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
  // re-run the coordination. Rows are immutable (`chatTimeline.js`): every
  // change arrives as a new rows array, so its identity is the change signal.
  // The controller coalesces through one animation frame — the same frame
  // boundary the browser scrolls on. Deferred so the read sees the controller
  // even when this effect runs ahead of its creation during mount.
  $effect(() => {
    timelineItems;
    transientCardIds;
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
    // Input that a nested scroll box (Tool output, diffs) moves leaves the
    // timeline where it is, so it must not release the follow pin.
    const movesTimelineUp = (event, upward) =>
      upward && !nestedScrollConsumes(event.target, container, true);
    const handleWheel = (event) => {
      controller?.noteUserInput({
        upward: movesTimelineUp(event, event.deltaY < 0),
      });
    };
    const handleTouchStart = (event) => {
      controller?.noteUserInput();
      touchY = event.touches?.[0]?.clientY ?? null;
    };
    const handleTouchMove = (event) => {
      const nextTouchY = event.touches?.[0]?.clientY ?? null;
      controller?.noteUserInput({
        upward: movesTimelineUp(
          event,
          touchY !== null && nextTouchY !== null && nextTouchY > touchY,
        ),
      });
      touchY = nextTouchY;
    };
    const handleKeyDown = (event) => {
      controller?.noteUserInput({
        upward: movesTimelineUp(event, isUpwardScrollKey(event.key)),
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
    lightboxImage = {
      kind: 'image',
      src: image.currentSrc || image.src,
      filename: image.alt,
    };
  }

  function closeLightbox() {
    lightboxImage = null;
  }

  function isReasoningOpen(id) {
    return viewState.isOpen(`reasoning:${id}`);
  }

  export async function revealSearchHit(destination, current) {
    await tick();
    if (!current() || !controller) return false;
    const found = locateSearchHit(timelineItems, destination.hit);
    if (!found) return false;
    const { row, child, message } = found;
    if (child && workingMode === 'compact') {
      const group = groupRunChildren(
        visibleRunChildren(row),
        workingMode,
        presentation === 'compact',
      ).find((group) => group.children?.includes(child));
      if (group) viewState.setOpen(group.id, true);
    }
    if (message.role === 'user') {
      // Steer renders its own message entry inside the Run.
      const key = child ? message.id : row.id;
      viewState.setOpen(`user-text:${key}`, true);
      userContentBlocks(message).forEach((block, index) =>
        viewState.setOpen(
          `${block.type === 'text' ? 'user-text' : 'user-transcription'}-${index}:${key}`,
          true,
        ),
      );
    }
    controller.revealRow(row.id);
    await tick();
    if (!current()) return false;
    const rowElement = Array.from(
      timelineContent.querySelectorAll('[data-timeline-item-id]'),
    ).find((el) => el.dataset.timelineItemId === row.id);
    const element = Array.from(
      rowElement?.querySelectorAll('[data-search-message]') ?? [],
    ).find((el) => el.dataset.searchMessage === message.id);
    if (!rowElement) return false;
    // A contribution can hide the built-in Markdown body. Its timeline
    // row is still a valid destination for this persisted message.
    const anchor = element ?? rowElement;
    controller.alignSearchMatch(anchor, searchRange(anchor));
    if (!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
      anchor.animate?.(
        [
          { backgroundColor: 'var(--surface-2)' },
          { backgroundColor: 'transparent' },
        ],
        { duration: 800 },
      );
    return true;
  }

  function setReasoningOpen(id, isOpen) {
    viewState.setOpen(`reasoning:${id}`, isOpen);
  }

  function shouldRenderTimelineDateSeparator(itemIndex) {
    if (!shouldShowTimelineDateSeparators || itemIndex === undefined) {
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
      sourceTimelineItems.length > 0 &&
      Boolean(scrollContainer)
    );
  }
</script>

{#snippet timelineContribution()}
  {#if timelineItems.length === 0 && transientCards.length === 0}
    {#if loadingHistory}
      <!-- While history is loading, a quiet placeholder — flashing the
               "No messages yet" empty state would be a lie for a session whose
               messages just have not arrived yet. -->
      <Banner variant="neutral" class="chat-timeline-loading">
        {t('loading.history')}
      </Banner>
    {:else if historyLoaded}
      <EmptyState
        fill
        class="chat-timeline-empty"
        title={conversation && sourceTimelineItems.length
          ? t('chat.noConversationMessages')
          : t('chat.historyEmptyTitle')}
        description={readOnly || sourceTimelineItems.length
          ? ''
          : t('chat.historyEmpty')}
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
    {#each renderEntries as entry (entry.key)}
      {#if entry.spacer}
        <div
          class="timeline-spacer"
          data-timeline-spacer
          aria-hidden="true"
          style:height={`${entry.height}px`}
        ></div>
      {:else}
        {@render timelineRow(entry.item)}
      {/if}
    {/each}
    {#each transientCardGroups.trailing as card (card.id)}
      {@render transientCard(card)}
    {/each}
  {/if}
{/snippet}

<div class="chat-timeline">
  <section class="messages" bind:this={scrollContainer}>
    <div class="messages__content" bind:this={timelineContent}>
      <ContributionSurface
        surface="chat.timeline"
        context={{
          ...uiContext,
          run_id: currentRun?.runId ?? uiContext.run_id ?? '',
          run_active: uiContext.run_active ?? currentRun?.status === 'running',
        }}
        data={{ items: presentedItems }}
        commands={{ 'timeline.latest': () => jumpToLatest() }}
        builtins={[
          {
            id: 'core:chat.timeline',
            title: t('chat.title'),
            order: 10,
            render: timelineContribution,
          },
        ]}
      />
    </div>
  </section>
  {#if showJumpToLatest}
    <Button
      variant="secondary"
      icon
      class="chat-timeline__jump-latest"
      ariaLabel={t('chat.jumpToLatest')}
      tooltip={t('chat.jumpToLatest')}
      onClick={jumpToLatest}
    >
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path d="m6 9 6 6 6-6M12 4v11" />
      </svg>
    </Button>
  {/if}
  <div class="chat-timeline__announcer" role="status" aria-live="polite">
    {announcement}
  </div>
</div>

<ContextMenu menu={subAgentMenu} onClose={() => (subAgentMenu = null)} />

{#snippet timelineRow(item)}
  {@const itemIndex = itemIndexById.get(item.id)}
  <div class="timeline-item" data-timeline-item-id={item.id}>
    {#if shouldRenderTimelineDateSeparator(itemIndex)}
      <div class="date-sep">
        {formatDate(timestampForItem(item))}
      </div>
    {/if}
    {#if item.type === 'run_status'}
      <div class="run-footer">
        {item.status === 'running'
          ? t('chat.working.active')
          : labelForEvent({ type: `run_${item.status}` })}
      </div>
    {:else if item.type === 'history_gap'}
      <div
        class="run-history-gap"
        aria-hidden="true"
        data-history-gap={item.run_id}
        data-history-gap-before={item.before}
      ></div>
    {:else if item.type === 'assistant_run'}
      <ChatAssistantRun
        {uiContext}
        {item}
        {agentName}
        chatWorkingMode={workingMode}
        summarizeSingleSteps={presentation === 'compact'}
        {subAgentStatuses}
        {backgroundCommandStatuses}
        {commandStatuses}
        sessionWaiting={sessionWaiting &&
          (item.sourceItemId ?? item.id) === latestRunItemId}
        sessionWorkStartedAt={(item.sourceItemId ?? item.id) === latestRunItemId
          ? sessionWorkStartedAt
          : null}
        footerRuns={footerGroups.get(item.sourceItemId ?? item.id) ?? null}
        {nowMs}
        {isReasoningOpen}
        onReasoningOpenChange={setReasoningOpen}
        {onNavigateToSubAgent}
        onSubAgentContextMenu={otherArea ? openSubAgentMenu : null}
        {onCancelToolCall}
        {onStopBackgroundCommand}
        {onBackgroundToolCall}
        loadToolCall={onLoadToolCall}
        backgroundToolCallIds={currentRun?.runId === item.runId &&
        currentRun.status === 'running'
          ? (currentRun.controls?.background_tool_call_ids ?? [])
          : []}
        {onCancelSubAgent}
        {messageEditingDisabledReason}
        {onEditMessage}
      />
    {:else}
      <ChatTimelineEntry
        {uiContext}
        {item}
        {agentName}
        {isReasoningOpen}
        onReasoningOpenChange={setReasoningOpen}
        {messageEditingDisabledReason}
        {onEditMessage}
        {onNavigateToSubAgent}
        onSubAgentContextMenu={otherArea ? openSubAgentMenu : null}
      />
    {/if}
    {#each transientCardGroups.byItemId.get(item.id) ?? [] as card (card.id)}
      {@render transientCard(card)}
    {/each}
    {#each transientCardGroups.byItemIndex.get(itemIndex) ?? [] as card (card.id)}
      {@render transientCard(card)}
    {/each}
  </div>
{/snippet}

{#snippet transientCard(card)}
  <div
    class="transient-card"
    role="note"
    aria-label={t('chat.transientCard.label')}
  >
    <div class="transient-card__header">
      <span class="transient-card__label">
        {t('chat.transientCard.label')}
      </span>
      <CopyButton
        text={card.text}
        class="chat-copy-action transient-card__copy"
        label={t('chat.copyCommandOutput')}
        copiedLabel={t('chat.commandOutputCopied')}
      />
    </div>
    <pre class="transient-card__body">{card.text}</pre>
  </div>
{/snippet}

{#if lightboxImage}
  <MediaLightbox items={[lightboxImage]} onClose={closeLightbox} />
{/if}
