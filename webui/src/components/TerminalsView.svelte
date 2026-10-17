<script>
  import TerminalDialogs from './terminals/TerminalDialogs.svelte';
  import {
    groupCanEdit,
    launchHistoryLabel,
    launchHistoryWorkdir,
    terminalDetails,
    terminalError,
    terminalGroupDetails,
    terminalLabel,
    terminalTitle,
    viewLabel,
  } from './terminals/terminalLabels.js';
  import './terminals/terminals.css';
  import { onMount, tick, untrack } from 'svelte';
  import { createTerminalRenderer } from './terminals/terminalRenderer.svelte.js';
  import '@xterm/xterm/css/xterm.css';

  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import ContextMenu from './ui/ContextMenu.svelte';
  import { contextMenuAnchor, isContextMenuKey } from './ui/contextMenu.js';

  import EmptyState from './ui/EmptyState.svelte';

  import { t } from '$lib/i18n.js';
  import { formatAgentAddress } from '$lib/agentAddress.js';
  import {
    TERMINAL_STREAM_CONNECTED,
    TERMINAL_STREAM_IDLE,
    canMoveTo,
    createTerminalsController,
    createTerminalsViewState,
    defaultTerminalGroupId,
    finishedTerminals,
    layoutForCount,
    pinTargetOf,
    shownViews,
    terminalIsFinished,
    visibleTerminals,
  } from '$lib/terminalsView.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    createStandaloneNavigation,
    useNavigation,
  } from '$lib/navigation.svelte.js';

  let {
    // The place is the shown view; an empty place shows the default view.
    navigation = createStandaloneNavigation(),
    terminalsRefreshToken = 0,
    serverUnavailable = false,
    onToast = () => {},
    // Opens a conversation in Chat: (agent address, session id).
    onOpenSession = null,
  } = $props();

  let dialogs = $state(null);
  let viewState = $state(createTerminalsViewState());

  let maximizedTerminalId = $state('');
  const shell = useNavigation();

  // The one open context menu of the view: a pill's, the start options,
  // the history, the "more views" list, or a tile's.
  let menu = $state(null);
  let draggedTerminalId = $state('');
  let dragOverTerminalId = $state('');
  let dragOverGroupId = $state('');
  let mounted = false;

  // Pills that do not fit wrap onto a clipped second line and wait in the
  // "more" menu; the shown view keeps its place behind the fitting ones.
  let rosterElement = $state(null);
  let viewsElement = $state(null);
  let fittingCount = $state(Number.POSITIVE_INFINITY);
  const ROSTER_INSET = 2;

  const groupTerminals = $derived(visibleTerminals(viewState));
  let hasTerminals = $derived(groupTerminals.length > 0);
  let layout = $derived(
    maximizedTerminalId
      ? layoutForCount(1)
      : layoutForCount(groupTerminals.length),
  );
  let selectedGroup = $derived(
    viewState.groups.find(
      (group) => group.group_id === viewState.selectedGroupId,
    ) ?? null,
  );
  const views = $derived(shownViews(viewState));
  const hiddenViews = $derived(views.slice(fittingCount));
  const selectedHidden = $derived(
    hiddenViews.some((group) => group.group_id === viewState.selectedGroupId),
  );
  const finished = $derived(finishedTerminals(viewState));
  const historyTerminal = $derived(
    viewState.historyTerminalId
      ? findTerminal(viewState.historyTerminalId)
      : null,
  );
  // History shows one terminal; it keeps its place in its own view.
  const reorderable = $derived(!historyTerminal && !maximizedTerminalId);

  const renderer = createTerminalRenderer({
    viewState,
    findTerminal,
    getController: () => controller,
    isMounted: () => mounted,
    isUnavailable: () => serverUnavailable,
    getMaximizedTerminalId: () => maximizedTerminalId,
  });
  const { mountTile, scrollToLatest } = renderer;
  const controller = createTerminalsController({
    state: viewState,
    onSnapshot: renderer.onSnapshot,
    onOutput: renderer.onOutput,
    onGeometry: renderer.onGeometry,
    onClear: renderer.onClear,
    onTranscript: renderer.onTranscript,
    onSpeechError: (error, phase) => {
      onToast({
        title:
          phase === 'requesting'
            ? t('terminals.voice.startFailed')
            : t('terminals.voice.transcriptionFailed'),
        message: error?.message ?? '',
        variant: 'error',
      });
    },
  });

  export function getVoiceContext() {
    return {
      selected_group_id: viewState.selectedGroupId,
      selected_terminal_id: viewState.selectedTerminalId,
      maximized_terminal_id: maximizedTerminalId,
      visible_order: groupTerminals.map((item) => item.terminal_id),
    };
  }

  export async function applyVoiceAction(action, args = {}) {
    if (serverUnavailable) throw new Error('server_unavailable');
    await controller.loadTerminals();
    if (viewState.listError) throw new Error('terminal_refresh_failed');
    if (action === 'context' || action === 'refresh') return getVoiceContext();
    if (action === 'restore') {
      if (maximizedTerminalId) await toggleMaximize(maximizedTerminalId);
      return getVoiceContext();
    }
    if (action === 'show_group') {
      if (!viewState.groups.some((group) => group.group_id === args.group_id))
        throw new Error('group_not_found');
      showGroup(args.group_id);
      controller.closeHistory();
      maximizedTerminalId = '';
      await tick();
      return getVoiceContext();
    }
    const target = viewState.terminals.find(
      (item) => item.terminal_id === args.terminal_id,
    );
    if (!target) throw new Error('terminal_not_found');
    showGroup(target.group_id);
    if (
      !visibleTerminals(viewState).some(
        (item) => item.terminal_id === target.terminal_id,
      )
    ) {
      controller.openHistory(target.terminal_id);
    }
    controller.selectTerminal(target.terminal_id);
    if (action === 'maximize' && maximizedTerminalId !== target.terminal_id) {
      await toggleMaximize(target.terminal_id);
    } else if (action === 'show' && maximizedTerminalId) {
      await toggleMaximize(maximizedTerminalId);
    }
    await tick();
    activateTerminal(target.terminal_id);
    return getVoiceContext();
  }

  $effect(() => {
    void terminalsRefreshToken;
    if (mounted && !serverUnavailable) {
      void controller.loadTerminals({ silent: true });
    }
  });

  $effect(() => {
    controller.setServerUnavailable(serverUnavailable);
  });

  // Place -> shown view. Before the first load the target waits in the
  // selection, which the load keeps while that view exists; a view that no
  // longer exists leaves the shown one in place and corrects the entry.
  $effect(() => {
    const groupId = navigation.place[0] ?? '';
    const loaded = viewState.groups.length > 0;
    untrack(() => {
      if (!loaded) {
        viewState.selectedGroupId = groupId;
        return;
      }
      controller.selectGroup(groupId || defaultTerminalGroupId(viewState));
      showGroupInPlace(viewState.selectedGroupId, { step: false });
    });
  });

  // Shown view -> place. A view that appears with this change (just created
  // or started into) is a new step; any other change - a load, a removed
  // view - corrects the current entry.
  let knownGroupIds = null;
  $effect(() => {
    const groupId = viewState.selectedGroupId;
    const groupIds = viewState.groups.map((group) => group.group_id);
    if (groupIds.length === 0) return;
    untrack(() => {
      const step = knownGroupIds !== null && !knownGroupIds.has(groupId);
      knownGroupIds = new Set(groupIds);
      showGroupInPlace(groupId, { step });
    });
  });

  function showGroupInPlace(groupId, { step }) {
    if ((navigation.place[0] ?? '') === groupId) return;
    const place = groupId ? [groupId] : [];
    if (step) navigation.navigate(place);
    else navigation.replace(place);
  }

  // Voice requests switch views as a step, like a click on a pill.
  function showGroup(groupId) {
    controller.selectGroup(groupId);
    showGroupInPlace(viewState.selectedGroupId, { step: true });
  }

  // A click on the shown pill also leaves the history.
  function selectView(groupId) {
    if (groupId === viewState.selectedGroupId) {
      controller.closeHistory();
      return;
    }
    navigation.navigate([groupId]);
  }

  // Back restores a maximized terminal before it navigates.
  $effect(() => {
    const terminalId = maximizedTerminalId;
    if (!terminalId) return;
    return shell?.registerLayer({ close: () => toggleMaximize(terminalId) });
  });

  // Back leaves a terminal opened from the history.
  $effect(() => {
    if (!viewState.historyTerminalId) return;
    return shell?.registerLayer({ close: () => controller.closeHistory() });
  });

  $effect(() => {
    if (
      maximizedTerminalId &&
      !groupTerminals.some((item) => item.terminal_id === maximizedTerminalId)
    ) {
      maximizedTerminalId = '';
    }
  });

  $effect(() => {
    void layout.columns;
    void layout.rows;
    void groupTerminals.length;
    void maximizedTerminalId;
    void tick().then(() => {
      renderer.fitAll();
    });
  });

  $effect(() => renderer.updateInteractivity(groupTerminals));

  function measureFittingViews() {
    if (!rosterElement) {
      return;
    }
    rosterElement.style.width = '';
    const pills = Array.from(rosterElement.children);
    const firstLine = pills[0]?.offsetTop ?? 0;
    const wrapped = pills.findIndex((pill) => pill.offsetTop !== firstLine);
    fittingCount = wrapped === -1 ? pills.length : wrapped;
    if (wrapped > 0) {
      const last = pills[wrapped - 1];
      const end = last.offsetLeft + last.offsetWidth - rosterElement.offsetLeft;
      rosterElement.style.width = `${end + ROSTER_INSET}px`;
    }
  }

  $effect(() => {
    if (!viewsElement || typeof ResizeObserver === 'undefined') {
      return undefined;
    }
    void views;
    const observer = new ResizeObserver(() => measureFittingViews());
    observer.observe(viewsElement);
    return () => observer.disconnect();
  });

  $effect(() => {
    // Names, counts, dots and the extra pill change what fits.
    void views
      .map(
        (group) =>
          `${viewLabel(group)}:${group.live_count}:${group.working_count}`,
      )
      .join();
    void selectedHidden;
    void rosterElement;
    tick().then(measureFittingViews);
  });

  onMount(() => {
    mounted = true;
    if (!serverUnavailable) {
      void controller.start();
    }

    return () => {
      mounted = false;
      controller.destroy();
      renderer.destroy();
    };
  });

  async function toggleMaximize(terminalId) {
    if (maximizedTerminalId === terminalId) {
      maximizedTerminalId = '';
    } else {
      if (viewState.speechTerminalId !== terminalId) controller.cancelSpeech();
      maximizedTerminalId = terminalId;
      controller.selectTerminal(terminalId);
    }
    await tick();
    renderer.fitAll();
  }

  function activateTerminalFromPointer(event, terminalId) {
    if (event.button !== 0 || !terminalId || serverUnavailable) {
      return;
    }
    activateTerminal(terminalId);
  }

  function activateTerminalFromKeyboard(event, terminalId) {
    if (
      !terminalId ||
      serverUnavailable ||
      event.target !== event.currentTarget
    ) {
      return;
    }
    if (event.key !== 'Enter' && event.key !== ' ') {
      return;
    }
    event.preventDefault();
    activateTerminal(terminalId);
  }

  function activateTerminal(terminalId) {
    const item = findTerminal(terminalId);
    if (!item) {
      return;
    }
    if (terminalId !== viewState.selectedTerminalId) {
      controller.selectTerminal(terminalId);
    }
    if (terminalIsFinished(item) || serverUnavailable) {
      return;
    }
    renderer.focus(terminalId);
  }

  // Close one tile with a single click. The controller removes the tile
  // immediately and runs the server-side stop + catalog removal in the
  // background, so the UI never waits for the process tree to die. A
  // finished terminal is only forgotten. No confirmation — the X is the
  // intent.
  function closeTerminal(terminalId) {
    const item = findTerminal(terminalId);
    if (!item) {
      return;
    }
    const wasRunning = !terminalIsFinished(item);
    void controller.closeTerminal(terminalId);
    onToast({
      title: t('terminals.closedTitle'),
      message: wasRunning
        ? t('terminals.closedMessage')
        : t('terminals.closedMessageHistory'),
      variant: 'success',
    });
  }

  function findTerminal(terminalId) {
    return (
      viewState.terminals.find(
        (terminal) => terminal.terminal_id === terminalId,
      ) ?? null
    );
  }

  function findGroup(groupId) {
    return viewState.groups.find((group) => group.group_id === groupId) ?? null;
  }

  // One click starts a terminal in the shown view: the default shell, or a
  // recent setup from the start menu. Its tile appearing is the feedback.
  async function quickStart(entry = null) {
    const params = {};
    if (entry?.command) params.command = entry.command;
    if (entry?.args?.length) params.args = [...entry.args];
    if (entry?.workdir) params.workdir = entry.workdir;
    maximizedTerminalId = '';
    const started = await controller.startManualTerminal(params, {
      groupId: pinTargetOf(selectedGroup),
    });
    if (!started) {
      onToast({
        title: t('terminals.startFailedTitle'),
        message: terminalError(viewState.startError),
        variant: 'error',
      });
      return;
    }
    await tick();
    activateTerminal(started.terminal_id);
  }

  function openSession(group) {
    const session = group?.session;
    if (!onOpenSession || !session?.agent_id || !session.session_id) return;
    onOpenSession(
      formatAgentAddress(session.agent_id, session.project_id),
      session.session_id,
    );
  }

  function openMenu(event, label, items) {
    event.preventDefault();
    event.stopPropagation();
    menu = { ...contextMenuAnchor(event), label, items };
  }

  function viewMenuItems(group) {
    if (groupCanEdit(group)) {
      return [
        {
          id: 'rename',
          label: t('terminals.renameGroupAction'),
          onSelect: () => dialogs.openRenameGroupDialog(group),
        },
        {
          id: 'delete',
          label: t('terminals.deleteGroupAction'),
          danger: true,
          onSelect: () => dialogs.openDeleteGroupDialog(group),
        },
      ];
    }
    if (group?.kind === 'session' && onOpenSession) {
      return [
        {
          id: 'open',
          label: t('terminals.openConversation'),
          onSelect: () => openSession(group),
        },
      ];
    }
    return [];
  }

  function openViewMenu(event, group) {
    const items = viewMenuItems(group);
    if (items.length === 0) return;
    openMenu(event, viewLabel(group), items);
  }

  function openStartMenu(event) {
    openMenu(event, t('terminals.startOptions'), [
      ...viewState.launchHistory.map((entry) => ({
        id: `history:${entry.id}`,
        label: launchHistoryLabel(entry),
        hint: launchHistoryWorkdir(entry),
        group: 'history',
        onSelect: () => void quickStart(entry),
      })),
      {
        id: 'customize',
        label: t('terminals.customize'),
        group: 'more',
        onSelect: () => dialogs.openStartDialog(),
      },
      {
        id: 'group',
        label: t('terminals.newGroup'),
        group: 'more',
        onSelect: () => dialogs.openCreateGroupDialog(),
      },
    ]);
  }

  function openHistoryMenu(event) {
    openMenu(
      event,
      t('terminals.history'),
      finished.map((item) => ({
        id: item.terminal_id,
        label: terminalTitle(item),
        hint: viewLabel(findGroup(item.group_id)),
        onSelect: () => {
          maximizedTerminalId = '';
          controller.openHistory(item.terminal_id);
        },
      })),
    );
  }

  function openMoreViewsMenu(event) {
    openMenu(
      event,
      t('terminals.moreViewsLabel'),
      hiddenViews.map((group) => ({
        id: group.group_id,
        label: viewLabel(group),
        hint: group.live_count ? String(group.live_count) : '',
        onSelect: () => selectView(group.group_id),
      })),
    );
  }

  function openTileMenu(event, item) {
    const moves = viewState.groups
      .filter(
        (group) =>
          canMoveTo(item, group) && group.group_id !== item.home_group_id,
      )
      .map((group) => ({
        id: `move:${group.group_id}`,
        label: t('terminals.moveTo', { view: viewLabel(group) }),
        group: 'move',
        onSelect: () =>
          void controller.moveTerminal(item.terminal_id, group.group_id),
      }));
    if (item.pinned) {
      moves.push({
        id: 'home',
        label: t('terminals.moveHome'),
        group: 'move',
        onSelect: () => void controller.moveTerminal(item.terminal_id, null),
      });
    }
    openMenu(
      event,
      t('terminals.terminalMenu', { name: terminalTitle(item) }),
      [
        ...moves,
        {
          id: 'maximize',
          label:
            maximizedTerminalId === item.terminal_id
              ? t('terminals.restore')
              : t('terminals.maximize'),
          group: 'tile',
          onSelect: () => void toggleMaximize(item.terminal_id),
        },
        {
          id: 'close',
          label: t('terminals.close'),
          group: 'tile',
          danger: true,
          onSelect: () => closeTerminal(item.terminal_id),
        },
      ],
    );
  }

  function onTileDragStart(event, terminalId) {
    if (historyTerminal) {
      event.preventDefault();
      return;
    }
    draggedTerminalId = terminalId;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', terminalId);
  }

  function onTileDragOver(event, terminalId) {
    if (
      !reorderable ||
      !draggedTerminalId ||
      draggedTerminalId === terminalId
    ) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    dragOverTerminalId = terminalId;
  }

  function onTileDragEnd() {
    draggedTerminalId = '';
    dragOverTerminalId = '';
    dragOverGroupId = '';
  }

  function onTileDrop(event, targetTerminalId) {
    event.preventDefault();
    const sourceId =
      draggedTerminalId || event.dataTransfer.getData('text/plain');
    onTileDragEnd();
    if (!sourceId || sourceId === targetTerminalId || !reorderable) {
      return;
    }
    const ids = groupTerminals.map((terminal) => terminal.terminal_id);
    const fromIndex = ids.indexOf(sourceId);
    const toIndex = ids.indexOf(targetTerminalId);
    if (fromIndex < 0 || toIndex < 0) {
      return;
    }
    const reordered = [...ids];
    reordered.splice(fromIndex, 1);
    reordered.splice(toIndex, 0, sourceId);
    controller.reorderGroup(viewState.selectedGroupId, reordered);
  }

  // A tile dropped on a pill shows in that view from now on; dropped on the
  // view it started in, it returns there.
  function onPillDragOver(event, group) {
    const item = findTerminal(draggedTerminalId);
    if (!canMoveTo(item, group)) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    dragOverGroupId = group.group_id;
  }

  function onPillDragLeave(group) {
    if (dragOverGroupId === group.group_id) dragOverGroupId = '';
  }

  function onPillDrop(event, group) {
    event.preventDefault();
    const item = findTerminal(
      draggedTerminalId || event.dataTransfer.getData('text/plain'),
    );
    onTileDragEnd();
    if (!canMoveTo(item, group)) {
      return;
    }
    const target =
      group.group_id === item.home_group_id ? null : group.group_id;
    void controller.moveTerminal(item.terminal_id, target);
  }
</script>

{#snippet viewPill(group, extra = false, wrapped = false)}
  <button
    type="button"
    class="terminals-view__pill"
    class:terminals-view__pill--extra={extra}
    class:terminals-view__pill--wrapped={wrapped}
    class:terminals-view__pill--drop-target={dragOverGroupId === group.group_id}
    aria-pressed={group.group_id === viewState.selectedGroupId}
    aria-label={`${viewLabel(group)}: ${t('terminals.count', { count: group.live_count ?? 0 })}`}
    use:tooltip={() => terminalGroupDetails(group, viewState.terminals)}
    disabled={serverUnavailable}
    onclick={() => selectView(group.group_id)}
    oncontextmenu={(event) => openViewMenu(event, group)}
    onkeydown={(event) => {
      if (isContextMenuKey(event)) openViewMenu(event, group);
    }}
    ondragover={(event) => onPillDragOver(event, group)}
    ondragleave={() => onPillDragLeave(group)}
    ondrop={(event) => onPillDrop(event, group)}
  >
    {#if group.working_count > 0}
      <span
        class="terminals-view__pill-dot tab-indicator tab-indicator--running"
        aria-hidden="true"
      ></span>
    {/if}
    <span class="terminals-view__pill-name">{viewLabel(group)}</span>
    {#if group.live_count > 0}
      <span class="terminals-view__pill-count" aria-hidden="true">
        {group.live_count}
      </span>
    {/if}
  </button>
{/snippet}

<section class="terminals-view" aria-label={t('terminals.title')}>
  <header class="terminals-view__toolbar">
    <div
      bind:this={viewsElement}
      class="terminals-view__views"
      role="group"
      aria-label={t('terminals.groupsLabel')}
    >
      <div bind:this={rosterElement} class="terminals-view__roster">
        {#each views as group, index (group.group_id)}
          {@render viewPill(group, false, index >= fittingCount)}
        {/each}
      </div>
      {#if selectedHidden && selectedGroup}
        {@render viewPill(selectedGroup, true)}
      {/if}
      {#if hiddenViews.length > 0}
        <button
          type="button"
          class="terminals-view__pill terminals-view__pill--more"
          aria-haspopup="menu"
          aria-label={t('terminals.moreViewsLabel')}
          use:tooltip={t('terminals.moreViewsLabel')}
          onclick={openMoreViewsMenu}
        >
          {t('terminals.moreViews', { count: hiddenViews.length })}
        </button>
      {/if}
      {#if viewState.loading && viewState.groups.length === 0}
        <span class="terminals-view__group-status">
          {t('terminals.loading')}
        </span>
      {/if}
    </div>
    <div class="terminals-view__actions">
      {#if selectedGroup && groupCanEdit(selectedGroup)}
        <Button
          variant="tertiary"
          icon
          aria-haspopup="menu"
          ariaLabel={t('terminals.groupActions')}
          tooltip={t('terminals.groupActions')}
          disabled={serverUnavailable}
          onClick={(event) => openViewMenu(event, selectedGroup)}
        >
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <circle cx="3.5" cy="8" r="1.3" />
            <circle cx="8" cy="8" r="1.3" />
            <circle cx="12.5" cy="8" r="1.3" />
          </svg>
        </Button>
      {:else if selectedGroup?.kind === 'session' && onOpenSession}
        <Button
          variant="tertiary"
          icon
          ariaLabel={t('terminals.openConversation')}
          tooltip={t('terminals.openConversation')}
          onClick={() => openSession(selectedGroup)}
        >
          <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
            <path
              d="M3 3.5h10v7H7l-3 2.5v-2.5H3zM6 6.5h4M6 8.5h2.5"
              class="terminals-view__stroke"
            />
          </svg>
        </Button>
      {/if}
      <Button
        variant="tertiary"
        class="terminals-view__history-button"
        aria-haspopup="menu"
        ariaLabel={t('terminals.history')}
        tooltip={finished.length
          ? t('terminals.history')
          : t('terminals.historyEmpty')}
        disabled={finished.length === 0}
        onClick={openHistoryMenu}
      >
        <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <path
            d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9M2.5 2.5v2.6h2.6M8 5v3.2l2 1.3"
            class="terminals-view__stroke"
          />
        </svg>
        {#if finished.length > 0}
          <span class="terminals-view__history-count">{finished.length}</span>
        {/if}
      </Button>
      <div class="terminals-view__start">
        <Button
          variant="secondary"
          class="terminals-view__start-main"
          ariaLabel={t('terminals.new')}
          tooltip={t('terminals.new')}
          loading={viewState.startingTerminal}
          disabled={serverUnavailable}
          onClick={() => void quickStart()}
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="M8 3v10M3 8h10" class="terminals-view__stroke" />
          </svg>
          <span>{t('terminals.newShort')}</span>
        </Button>
        <Button
          variant="secondary"
          icon
          class="terminals-view__start-more"
          aria-haspopup="menu"
          ariaLabel={t('terminals.startOptions')}
          tooltip={t('terminals.startOptions')}
          disabled={serverUnavailable}
          onClick={openStartMenu}
        >
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path d="m4 6 4 4 4-4" class="terminals-view__stroke" />
          </svg>
        </Button>
      </div>
    </div>
  </header>

  <div class="terminals-view__detail">
    {#if viewState.listError && !serverUnavailable}
      <Banner variant="error" class="terminals-view__feedback">
        <span>{t('terminals.listError')}</span>
        <Button variant="secondary" onClick={() => controller.loadTerminals()}>
          {t('common.retry')}
        </Button>
      </Banner>
    {/if}
    {#if historyTerminal}
      <div class="terminals-view__history-bar" role="status">
        <span class="terminals-view__history-kind">
          {t('terminals.historyShowing')}
        </span>
        <span class="terminals-view__history-title">
          {terminalTitle(historyTerminal)}
        </span>
        <span class="terminals-view__history-view">
          {viewLabel(findGroup(historyTerminal.group_id))}
        </span>
        <Button
          variant="tertiary"
          class="terminals-view__history-back"
          onClick={() => controller.closeHistory()}
        >
          {t('terminals.historyBack')}
        </Button>
      </div>
    {/if}
    {#if hasTerminals}
      {#if viewState.actionError && !serverUnavailable}
        <Banner variant="error" class="terminals-view__feedback">
          <span>{terminalError(viewState.actionError)}</span>
        </Banner>
      {/if}

      <div
        class="terminals-view__canvas"
        class:terminals-view__canvas--maximized={maximizedTerminalId !== ''}
        role="group"
        aria-label={t('terminals.canvasLabel')}
        style="grid-template-columns: repeat({layout.columns}, minmax(0, 1fr)); grid-template-rows: repeat({layout.rows}, minmax(0, 1fr));"
      >
        {#each groupTerminals as item, itemIndex (item.terminal_id)}
          {@const span = layout.spans[itemIndex] ?? {
            row: 0,
            column: 0,
            rowSpan: 1,
            columnSpan: 1,
          }}
          {@const stream = viewState.streams[item.terminal_id] ?? {
            status: TERMINAL_STREAM_IDLE,
            error: '',
            errorCode: '',
          }}
          {@const isMaximized = maximizedTerminalId === item.terminal_id}
          {@const isFinished = terminalIsFinished(item)}
          {@const isFocused = item.terminal_id === viewState.selectedTerminalId}
          {@const hasSpeech = item.terminal_id === viewState.speechTerminalId}
          {@const isRecording =
            hasSpeech && viewState.speechState === 'recording'}
          {@const speechBusy = hasSpeech && !isRecording}
          {@const speechLabel = isRecording
            ? t('terminals.voice.stop')
            : t('terminals.voice.start')}
          {@const isDragged = draggedTerminalId === item.terminal_id}
          {@const isDropTarget = dragOverTerminalId === item.terminal_id}
          <div
            class="terminals-view__tile"
            class:terminals-view__tile--hidden={maximizedTerminalId &&
              !isMaximized}
            class:terminals-view__tile--maximized={isMaximized}
            class:terminals-view__tile--focused={isFocused &&
              !maximizedTerminalId}
            class:terminals-view__tile--finished={isFinished}
            class:terminals-view__tile--dragging={isDragged}
            class:terminals-view__tile--drop-target={isDropTarget}
            data-terminal-id={item.terminal_id}
            style="grid-row: {span.row +
              1} / span {span.rowSpan}; grid-column: {span.column +
              1} / span {span.columnSpan};"
          >
            <div
              class="terminals-view__tile-bar"
              class:terminals-view__tile-bar--busy={hasSpeech}
              role="button"
              tabindex="0"
              aria-label={terminalTitle(item)}
              draggable={!historyTerminal}
              ondragstart={(event) => onTileDragStart(event, item.terminal_id)}
              ondragover={(event) => onTileDragOver(event, item.terminal_id)}
              use:tooltip={() => terminalDetails(item)}
              ondragend={onTileDragEnd}
              ondrop={(event) => onTileDrop(event, item.terminal_id)}
              onclick={() => activateTerminal(item.terminal_id)}
              ondblclick={() => toggleMaximize(item.terminal_id)}
              oncontextmenu={(event) => openTileMenu(event, item)}
              onkeydown={(event) => {
                if (isContextMenuKey(event)) {
                  openTileMenu(event, item);
                } else if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  activateTerminal(item.terminal_id);
                } else if (event.key === 'F2') {
                  event.preventDefault();
                  toggleMaximize(item.terminal_id);
                }
              }}
            >
              <span
                class="terminals-view__tile-dot tab-indicator"
                class:tab-indicator--running={item.state === 'working'}
                aria-hidden="true"
              ></span>
              {#if item.call_name}
                <span class="terminals-view__tile-call">{item.call_name}</span>
              {/if}
              <span class="terminals-view__tile-label"
                >{terminalLabel(item)}</span
              >
              {#if item.pinned}
                <span
                  class="terminals-view__tile-pin"
                  use:tooltip={t('terminals.pinnedHint')}
                >
                  <svg
                    viewBox="0 0 16 16"
                    width="12"
                    height="12"
                    aria-label={t('terminals.pinnedHint')}
                  >
                    <path
                      d="M6 2.5h4l-.6 4 2.1 2H4.5l2.1-2zM8 8.5v5"
                      class="terminals-view__stroke"
                    />
                  </svg>
                </span>
              {/if}
              {#if isFinished}
                <span class="terminals-view__tile-state">
                  {Number.isInteger(item.exit_code)
                    ? t('terminals.state.exitedWithCode', {
                        code: item.exit_code,
                      })
                    : t('terminals.state.exited')}
                </span>
              {/if}
              {#if renderer.scrolledBack(item.terminal_id)}
                <button
                  type="button"
                  class="terminals-view__latest-action"
                  onclick={(event) => {
                    event.stopPropagation();
                    scrollToLatest(item.terminal_id);
                  }}
                >
                  {t('terminals.scrollLatest')}
                </button>
              {/if}
              <span
                class="terminals-view__tile-actions"
                role="presentation"
                onclick={(event) => event.stopPropagation()}
                ondblclick={(event) => event.stopPropagation()}
                onkeydown={(event) => event.stopPropagation()}
              >
                {#if !isFinished}
                  {#if hasSpeech}
                    <span class="terminals-view__speech-status" role="status">
                      {viewState.speechState === 'requesting'
                        ? t('terminals.voice.requesting')
                        : isRecording
                          ? t('voice.state.recording')
                          : t('voice.state.transcribing')}
                    </span>
                  {/if}
                  <Button
                    variant="tertiary"
                    icon
                    class={`terminals-view__tile-action ${isRecording ? 'btn-icon--active' : ''}`}
                    ariaLabel={speechLabel}
                    tooltip={speechLabel}
                    aria-pressed={isRecording}
                    loading={speechBusy}
                    disabled={serverUnavailable ||
                      stream.status !== TERMINAL_STREAM_CONNECTED ||
                      (!!viewState.speechTerminalId && !hasSpeech) ||
                      !renderer.hasRenderer(item.terminal_id)}
                    onClick={() =>
                      void controller.toggleSpeech(item.terminal_id)}
                  >
                    <svg
                      viewBox="0 0 16 16"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path
                        d="M8 2a2 2 0 0 1 2 2v4a2 2 0 1 1-4 0V4a2 2 0 0 1 2-2z"
                      />
                      <path d="M4 7v1a4 4 0 0 0 8 0V7M8 12v2M6 14h4" />
                    </svg>
                  </Button>
                  {#if hasSpeech}
                    <Button
                      variant="tertiary"
                      icon
                      class="terminals-view__tile-action"
                      ariaLabel={t('terminals.voice.cancel')}
                      tooltip={t('terminals.voice.cancel')}
                      onClick={() => controller.cancelSpeech(item.terminal_id)}
                    >
                      <svg
                        viewBox="0 0 14 14"
                        width="14"
                        height="14"
                        aria-hidden="true"
                      >
                        <path d="m4 4 6 6m0-6-6 6" />
                      </svg>
                    </Button>
                  {/if}
                {/if}
                <Button
                  variant="tertiary"
                  icon
                  class="terminals-view__tile-action"
                  ariaLabel={isMaximized
                    ? t('terminals.restore')
                    : t('terminals.maximize')}
                  tooltip={isMaximized
                    ? t('terminals.restoreHint')
                    : t('terminals.maximizeHint')}
                  onClick={() => toggleMaximize(item.terminal_id)}
                >
                  {#if isMaximized}
                    <svg
                      viewBox="0 0 14 14"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path d="M5.5 5.5V2.5h6v6h-3M2.5 5.5h6v6h-6z" />
                    </svg>
                  {:else}
                    <svg
                      viewBox="0 0 14 14"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path d="M3 3h8v8H3z" />
                    </svg>
                  {/if}
                </Button>
                <Button
                  variant="danger"
                  icon
                  class="terminals-view__tile-action"
                  ariaLabel={t('terminals.close')}
                  tooltip={t('terminals.close')}
                  onClick={() => void closeTerminal(item.terminal_id)}
                >
                  <svg
                    viewBox="0 0 14 14"
                    width="14"
                    height="14"
                    aria-hidden="true"
                  >
                    <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" />
                  </svg>
                </Button>
              </span>
            </div>
            <div class="terminals-view__tile-chrome" role="presentation">
              {#if stream.errorCode === 'gap' && !serverUnavailable}
                <Banner variant="warn" class="terminals-view__tile-feedback">
                  <span>
                    {t('terminals.streamGap')}
                  </span>
                </Banner>
              {/if}
              {#if stream.error && !serverUnavailable}
                <Banner variant="warn" class="terminals-view__tile-feedback">
                  <span>{terminalError(stream.error)}</span>
                </Banner>
              {/if}
              <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
              <div
                use:mountTile={item.terminal_id}
                class="terminals-view__tile-host"
                role="group"
                tabindex={isFinished ? -1 : 0}
                aria-label={isFinished
                  ? t('terminals.historyTerminalLabel')
                  : t('terminals.liveTerminalLabel')}
                onpointerdown={(event) =>
                  activateTerminalFromPointer(event, item.terminal_id)}
                onkeydown={(event) =>
                  activateTerminalFromKeyboard(event, item.terminal_id)}
              ></div>
            </div>
          </div>
        {/each}
      </div>
    {:else if !viewState.listError || serverUnavailable}
      <EmptyState
        fill
        title={selectedGroup
          ? t('terminals.groupEmptyTitle')
          : t('terminals.detailEmptyTitle')}
        description={selectedGroup?.kind === 'user'
          ? t('terminals.groupEmptyDescription')
          : t('terminals.detailEmptyDescription')}
      >
        {#snippet actions()}
          <Button
            variant="primary"
            ariaLabel={t('terminals.new')}
            loading={viewState.startingTerminal}
            disabled={serverUnavailable}
            onClick={() => void quickStart()}
          >
            {t('terminals.new')}
          </Button>
          <Button
            variant="secondary"
            disabled={serverUnavailable}
            onClick={() => dialogs.openStartDialog()}
          >
            {t('terminals.customize')}
          </Button>
        {/snippet}
      </EmptyState>
    {/if}
  </div>
</section>

<ContextMenu {menu} onClose={() => (menu = null)} />

<TerminalDialogs
  bind:this={dialogs}
  {viewState}
  {controller}
  {serverUnavailable}
  {onToast}
  onStarted={async (id) => {
    maximizedTerminalId = '';
    await tick();
    activateTerminal(id);
  }}
/>
