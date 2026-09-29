<script>
  import TerminalDialogs from './terminals/TerminalDialogs.svelte';
  import {
    groupCanEdit,
    terminalError,
    terminalTarget,
    terminalTitle,
  } from './terminals/terminalLabels.js';
  import './terminals/terminals.css';
  import { onMount, tick, untrack } from 'svelte';
  import { createTerminalRenderer } from './terminals/terminalRenderer.svelte.js';
  import '@xterm/xterm/css/xterm.css';

  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';

  import EmptyState from './ui/EmptyState.svelte';

  import { t } from '$lib/i18n.js';
  import {
    TERMINAL_STREAM_CONNECTED,
    TERMINAL_STREAM_IDLE,
    createTerminalsController,
    createTerminalsViewState,
    defaultTerminalGroupId,
    layoutForCount,
    terminalIsFinished,
    visibleTerminals,
  } from '$lib/terminalsView.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { tooltip } from '$lib/tooltip.js';
  import {
    createStandaloneNavigation,
    useNavigation,
  } from '$lib/navigation.svelte.js';

  let {
    // The place is the shown group; an empty place shows the default group.
    navigation = createStandaloneNavigation(),
    terminalsRefreshToken = 0,
    serverUnavailable = false,
    onToast = () => {},
  } = $props();

  let dialogs = $state(null);
  let viewState = $state(createTerminalsViewState());

  let maximizedTerminalId = $state('');
  const shell = useNavigation();

  // Single open "…" action menu per group, portaled to <body> like the
  // session row menu. Only one group menu is open at a time.
  let openGroupMenuId = $state(null);
  let groupMenuTriggerElement = $state(null);
  let groupMenuElement = $state(null);
  let groupMenuStyle = $state('visibility: hidden;');
  let groupMenuPlacement = $state('bottom');
  const GROUP_ACTION_MENU_FALLBACK_WIDTH = 160;
  let draggedTerminalId = $state('');
  let dragOverTerminalId = $state('');
  let mounted = false;

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
  const canStartInGroup = $derived(selectedGroup?.kind !== 'finished');
  const lastTileSpan = $derived(layout.spans[groupTerminals.length - 1]);
  const appendSharesCell = $derived(
    !!lastTileSpan &&
      lastTileSpan.column + lastTileSpan.columnSpan === layout.columns,
  );
  let groupReorderable = $derived(
    !!selectedGroup &&
      selectedGroup.kind !== 'finished' &&
      selectedGroup.kind !== 'automatic',
  );

  const renderer = createTerminalRenderer({
    viewState,
    findTerminal,
    getController: () => controller,
    isMounted: () => mounted,
    isUnavailable: () => serverUnavailable,
    getMaximizedTerminalId: () => maximizedTerminalId,
  });
  const { mountTile, scrollToLatest, gridMismatchHint } = renderer;
  const controller = createTerminalsController({
    state: viewState,
    onSnapshot: renderer.onSnapshot,
    onOutput: renderer.onOutput,
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
      maximizedTerminalId = '';
      await tick();
      return getVoiceContext();
    }
    const target = viewState.terminals.find(
      (item) => item.terminal_id === args.terminal_id,
    );
    if (!target) throw new Error('terminal_not_found');
    showGroup(target.group_id);
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

  // Place -> shown group. Before the first load the target waits in the
  // selection, which the load keeps while that group exists; a group that no
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

  // Shown group -> place. A group that appears with this change (just created
  // or started into) is a new step; any other change - a load, a removed
  // group - corrects the current entry.
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

  // Voice requests switch groups as a step, like a click on the group tab.
  function showGroup(groupId) {
    controller.selectGroup(groupId);
    showGroupInPlace(viewState.selectedGroupId, { step: true });
  }

  // Back restores a maximized terminal before it navigates.
  $effect(() => {
    const terminalId = maximizedTerminalId;
    if (!terminalId) return;
    return shell?.registerLayer({ close: () => toggleMaximize(terminalId) });
  });

  // An open group menu closes first, like on Escape.
  $effect(() => {
    if (openGroupMenuId === null) return;
    return shell?.registerLayer({ close: closeGroupMenu });
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

  function toggleGroupMenu(groupId, triggerElement) {
    if (openGroupMenuId === groupId) {
      closeGroupMenu();
      return;
    }
    openGroupMenuId = groupId;
    groupMenuTriggerElement = triggerElement;
    groupMenuStyle = 'visibility: hidden;';
    void tick().then(() => updateGroupMenuPosition());
  }

  function closeGroupMenu() {
    openGroupMenuId = null;
    groupMenuTriggerElement = null;
    groupMenuElement = null;
    groupMenuStyle = 'visibility: hidden;';
    groupMenuPlacement = 'bottom';
  }

  function updateGroupMenuPosition() {
    if (
      openGroupMenuId === null ||
      !groupMenuTriggerElement ||
      !groupMenuElement
    ) {
      return;
    }
    const menuRect = groupMenuElement.getBoundingClientRect();
    const { placement, left, width, verticalRule, optionsMaxHeight } =
      computePanelPosition(groupMenuTriggerElement, {
        contentHeight: groupMenuElement.scrollHeight || menuRect.height,
        panelWidth: menuRect.width || GROUP_ACTION_MENU_FALLBACK_WIDTH,
        horizontalAlign: 'end',
      });
    groupMenuPlacement = placement;
    groupMenuStyle = [
      `left: ${left}px`,
      verticalRule,
      `width: ${width}px`,
      `max-height: ${optionsMaxHeight}px`,
    ].join('; ');
  }

  function handleGroupMenuDocumentMouseDown(event) {
    if (openGroupMenuId === null) {
      return;
    }
    if (
      event.target instanceof Element &&
      (groupMenuTriggerElement?.contains(event.target) ||
        groupMenuElement?.contains(event.target))
    ) {
      return;
    }
    closeGroupMenu();
  }

  function handleGroupMenuDocumentKeyDown(event) {
    if (event.key === 'Escape') {
      closeGroupMenu();
    }
  }

  function onTileDragStart(event, terminalId) {
    if (!groupReorderable) {
      return;
    }
    draggedTerminalId = terminalId;
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', terminalId);
  }

  function onTileDragOver(event, terminalId) {
    if (
      !groupReorderable ||
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
  }

  function onTileDrop(event, targetTerminalId) {
    event.preventDefault();
    const sourceId =
      draggedTerminalId || event.dataTransfer.getData('text/plain');
    draggedTerminalId = '';
    dragOverTerminalId = '';
    if (!sourceId || sourceId === targetTerminalId || !groupReorderable) {
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
</script>

<svelte:document
  onmousedown={handleGroupMenuDocumentMouseDown}
  onkeydown={handleGroupMenuDocumentKeyDown}
/>
<svelte:window onresize={closeGroupMenu} />

<section class="terminals-view" aria-label={t('terminals.title')}>
  <header class="terminals-view__toolbar">
    <div
      class="terminals-view__group-tabs"
      aria-label={t('terminals.groupsLabel')}
      onscroll={closeGroupMenu}
    >
      {#each viewState.groups as group (group.group_id)}
        <div
          class="terminals-view__group-tab-wrap"
          class:terminals-view__group-tab-wrap--editable={groupCanEdit(group)}
          class:terminals-view__group-tab-wrap--start={group.group_id ===
            viewState.selectedGroupId && canStartInGroup}
        >
          <button
            type="button"
            class="terminals-view__group-tab"
            class:active={group.group_id === viewState.selectedGroupId}
            aria-current={group.group_id === viewState.selectedGroupId
              ? 'true'
              : undefined}
            aria-label={`${group.name}: ${t('terminals.count', { count: group.terminal_count })}`}
            use:tooltip={{ text: group.name, whenTruncated: true }}
            onclick={() => navigation.navigate([group.group_id])}
          >
            <span class="terminals-view__group-tab-label">
              <span class="terminals-view__group-tab-name">{group.name}</span>
              <span class="terminals-view__group-tab-count">
                {group.terminal_count}
              </span>
            </span>
          </button>
          {#if group.group_id === viewState.selectedGroupId && canStartInGroup}
            <span class="terminals-view__group-start">
              <Button
                variant="tertiary"
                icon
                ariaLabel={t('terminals.new')}
                tooltip={t('terminals.new')}
                disabled={serverUnavailable}
                onClick={() => dialogs.openStartDialog()}
              >
                <svg
                  viewBox="0 0 20 20"
                  width="18"
                  height="18"
                  aria-hidden="true"
                >
                  <path d="M11 16H3V4h14v5M6 8l3 2-3 2M15 11v8M11 15h8" />
                </svg>
              </Button>
            </span>
          {/if}
          {#if groupCanEdit(group)}
            <span class="terminals-view__group-actions">
              <button
                type="button"
                class="terminals-view__group-action-menu-trigger"
                class:terminals-view__group-action-menu-trigger--open={openGroupMenuId ===
                  group.group_id}
                aria-label={t('terminals.groupActions')}
                aria-haspopup="menu"
                aria-expanded={openGroupMenuId === group.group_id}
                onclick={(event) =>
                  toggleGroupMenu(group.group_id, event.currentTarget)}
              >
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <circle cx="8" cy="3" r="1.4" />
                  <circle cx="8" cy="8" r="1.4" />
                  <circle cx="8" cy="13" r="1.4" />
                </svg>
              </button>
              {#if openGroupMenuId === group.group_id}
                <div
                  bind:this={groupMenuElement}
                  use:portal
                  class="terminals-view__group-menu"
                  role="menu"
                  data-placement={groupMenuPlacement}
                  data-positioning="fixed"
                  style={groupMenuStyle}
                >
                  <button
                    type="button"
                    class="terminals-view__group-menu-item"
                    role="menuitem"
                    onclick={() => {
                      closeGroupMenu();
                      dialogs.openRenameGroupDialog(group);
                    }}
                  >
                    {t('terminals.renameGroupAction')}
                  </button>
                  <button
                    type="button"
                    class="terminals-view__group-menu-item terminals-view__group-menu-item--danger"
                    role="menuitem"
                    onclick={() => {
                      closeGroupMenu();
                      dialogs.openDeleteGroupDialog(group);
                    }}
                  >
                    {t('terminals.deleteGroupAction')}
                  </button>
                </div>
              {/if}
            </span>
          {/if}
        </div>
      {/each}
      <span class="terminals-view__add-group">
        <Button
          variant="tertiary"
          icon
          ariaLabel={t('terminals.addGroup')}
          tooltip={t('terminals.addGroup')}
          disabled={serverUnavailable}
          onClick={() => dialogs.openCreateGroupDialog()}
        >
          <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">
            <path d="M7 1v12M1 7h12" />
          </svg>
        </Button>
      </span>
      {#if viewState.loading && viewState.groups.length === 0}
        <span class="terminals-view__group-status">
          {t('terminals.loading')}
        </span>
      {/if}
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
    {:else if hasTerminals}
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
          {@const gridMismatch = gridMismatchHint(item.terminal_id)}
          <div
            class="terminals-view__tile"
            class:terminals-view__tile--hidden={maximizedTerminalId &&
              !isMaximized}
            class:terminals-view__tile--maximized={isMaximized}
            class:terminals-view__tile--focused={isFocused &&
              !maximizedTerminalId}
            class:terminals-view__tile--dragging={isDragged}
            class:terminals-view__tile--drop-target={isDropTarget}
            class:terminals-view__tile--append-space={canStartInGroup &&
              !maximizedTerminalId &&
              appendSharesCell &&
              itemIndex === groupTerminals.length - 1}
            data-terminal-id={item.terminal_id}
            style="grid-row: {span.row +
              1} / span {span.rowSpan}; grid-column: {span.column +
              1} / span {span.columnSpan};"
          >
            <div
              class="terminals-view__tile-bar"
              role="button"
              tabindex="0"
              aria-label={terminalTitle(item)}
              draggable={groupReorderable}
              ondragstart={(event) => onTileDragStart(event, item.terminal_id)}
              ondragover={(event) => onTileDragOver(event, item.terminal_id)}
              ondragend={onTileDragEnd}
              ondrop={(event) => onTileDrop(event, item.terminal_id)}
              onclick={() => activateTerminal(item.terminal_id)}
              ondblclick={() => toggleMaximize(item.terminal_id)}
              onkeydown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  activateTerminal(item.terminal_id);
                } else if (event.key === 'F2') {
                  event.preventDefault();
                  toggleMaximize(item.terminal_id);
                }
              }}
            >
              <div class="terminals-view__tile-bar-primary">
                <span
                  class="terminals-view__tile-title"
                  use:tooltip={{
                    text: terminalTitle(item),
                    whenTruncated: true,
                  }}>{terminalTitle(item)}</span
                >
                <span
                  class="terminals-view__tile-target"
                  use:tooltip={{
                    text: terminalTarget(item),
                    whenTruncated: true,
                  }}>{terminalTarget(item)}</span
                >
                {#if gridMismatch}
                  <span
                    class="terminals-view__grid-mismatch"
                    use:tooltip={t('terminals.gridMismatchHelp')}
                    >{gridMismatch}</span
                  >
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
                        onClick={() =>
                          controller.cancelSpeech(item.terminal_id)}
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
                      ? t('terminals.restore')
                      : t('terminals.maximize')}
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
        {#if canStartInGroup && !maximizedTerminalId && lastTileSpan}
          <Button
            variant="tertiary"
            class={`terminals-view__append-tile ${appendSharesCell ? 'terminals-view__append-tile--shared' : ''}`}
            style="grid-row: {lastTileSpan.row +
              1}; grid-column: {appendSharesCell
              ? lastTileSpan.column + 1
              : lastTileSpan.column + lastTileSpan.columnSpan + 1};"
            ariaLabel={t('terminals.new')}
            tooltip={t('terminals.new')}
            disabled={serverUnavailable}
            onClick={() => dialogs.openStartDialog()}
          >
            <svg viewBox="0 0 14 14" width="18" height="18" aria-hidden="true">
              <path d="M7 1v12M1 7h12" />
            </svg>
          </Button>
        {/if}
      </div>
    {:else}
      <EmptyState
        fill
        title={selectedGroup
          ? t('terminals.groupEmptyTitle')
          : t('terminals.detailEmptyTitle')}
        description={selectedGroup
          ? t('terminals.groupEmptyDescription')
          : t('terminals.detailEmptyDescription')}
      >
        {#snippet actions()}
          {#if canStartInGroup}
            <Button
              variant="primary"
              ariaLabel={t('terminals.new')}
              disabled={serverUnavailable}
              onClick={() => dialogs.openStartDialog()}
            >
              {t('terminals.new')}
            </Button>
          {/if}
        {/snippet}
      </EmptyState>
    {/if}
  </div>
</section>

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
