<script>
  import { onMount, tick } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import '@xterm/xterm/css/xterm.css';

  import Banner from './ui/Banner.svelte';
  import Button from './ui/Button.svelte';
  import Dropdown from './Dropdown.svelte';
  import EmptyState from './ui/EmptyState.svelte';
  import FormField from './ui/FormField.svelte';
  import Modal from './ui/Modal.svelte';
  import TextArea from './ui/TextArea.svelte';
  import TextField from './ui/TextField.svelte';
  import { t } from '$lib/i18n.js';
  import {
    TERMINAL_MAX_COLUMNS,
    TERMINAL_MAX_ROWS,
    TERMINAL_STREAM_ERROR,
    TERMINAL_STREAM_IDLE,
    clampTerminalGrid,
    createTerminalsController,
    createTerminalsViewState,
    layoutForCount,
    terminalIsFinished,
    visibleTerminals,
  } from '$lib/terminalsView.js';
  import { computePanelPosition, portal } from '$lib/dropdownPanel.js';
  import { tooltip } from '$lib/tooltip.js';

  let {
    terminalsRefreshToken = 0,
    serverUnavailable = false,
    onToast = () => {},
  } = $props();

  let viewState = $state(createTerminalsViewState());
  let pendingFocusTerminalId = '';
  let maximizedTerminalId = $state('');
  let scrolledBackByTerminal = $state({});
  let startDialogOpen = $state(false);
  let selectedLaunchHistoryId = $state('');
  let startCommand = $state('');
  let startArguments = $state('');
  let startWorkdir = $state('');
  let startName = $state('');
  let startGroupId = $state('');
  let groupDialogOpen = $state(false);
  let groupDialogMode = $state('create');
  let groupDialogName = $state('');
  let groupDialogTargetId = $state('');
  let deleteGroupDialogOpen = $state(false);
  let deleteGroupTargetId = $state('');
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
  let xtermModulesPromise = null;
  const tileRegistry = new SvelteMap();
  const tileHosts = new SvelteMap();
  const rendererPromises = new SvelteMap();
  const pendingSnapshots = new SvelteMap();
  const pendingOutputs = new SvelteMap();
  // The grid each tile is currently fitted to, kept as reactive state so the
  // per-tile diagnostics hint can compare it against the server dimensions.
  let fittedGrids = $state({});
  const TERMINAL_BASE_FONT_SIZE = 12;
  // WebGL is intentionally not used: its renderer sizes the canvas backing
  // store from the browser's device-pixel box, which sits one pixel off the
  // cell grid at fractional desktop scale (Windows 125% / 150%) and paints
  // regular white row gaps that no post-hoc detection could fully rule out.
  // The DOM renderer is deterministic and fast enough for a handful of tiles.

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
  let groupReorderable = $derived(
    !!selectedGroup &&
      selectedGroup.kind !== 'finished' &&
      selectedGroup.kind !== 'automatic',
  );
  let launchHistoryOptions = $derived(
    viewState.launchHistory.map((entry) => ({
      value: entry.id,
      label: launchHistoryLabel(entry),
      secondaryLabel: launchHistoryWorkdir(entry),
    })),
  );
  let groupOptions = $derived(
    viewState.groups
      .filter((group) => group.kind === 'user' || group.kind === 'agent')
      .map((group) => ({
        value: group.group_id,
        label: group.name,
        secondaryLabel: groupKindLabel(group.kind),
      })),
  );
  const groupOptionAutomatic = {
    value: '',
    label: t('terminals.groupAutomatic', 'Automatic'),
    secondaryLabel: t('terminals.kind.manual', 'Manual'),
  };

  const controller = createTerminalsController({
    state: viewState,
    onSnapshot: (terminalId, ansi, snapshotTerminal) => {
      pendingSnapshots.set(terminalId, {
        ansi,
        terminal: snapshotTerminal,
      });
      pendingOutputs.delete(terminalId);
      const tile = tileRegistry.get(terminalId);
      if (!tile) {
        return;
      }
      writeSnapshot(terminalId, ansi, snapshotTerminal);
    },
    onOutput: (terminalId, data) => {
      const tile = tileRegistry.get(terminalId);
      if (tile) {
        tile.xterm.write(data);
      } else {
        const queued = pendingOutputs.get(terminalId) ?? [];
        queued.push(data);
        pendingOutputs.set(terminalId, queued.slice(-256));
      }
    },
    onClear: (terminalId) => {
      pendingSnapshots.delete(terminalId);
      pendingOutputs.delete(terminalId);
      scrolledBackByTerminal[terminalId] = false;
      tileRegistry.get(terminalId)?.xterm?.reset();
    },
  });

  $effect(() => {
    void terminalsRefreshToken;
    if (mounted && !serverUnavailable) {
      void controller.loadTerminals({ silent: true });
    }
  });

  $effect(() => {
    controller.setServerUnavailable(serverUnavailable);
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
      for (const id of tileRegistry.keys()) {
        scheduleFit(id);
      }
    });
  });

  $effect(() => {
    for (const item of groupTerminals) {
      const tile = tileRegistry.get(item.terminal_id);
      if (!tile?.xterm) {
        continue;
      }
      const interactive = !terminalIsFinished(item) && !serverUnavailable;
      tile.xterm.options.disableStdin = !interactive;
      tile.xterm.options.cursorBlink = interactive;
    }
  });

  onMount(() => {
    mounted = true;
    if (!serverUnavailable) {
      void controller.start();
    }

    return () => {
      mounted = false;
      controller.destroy();
      for (const terminalId of [...tileRegistry.keys()]) {
        disposeTile(terminalId);
      }
    };
  });

  function mountTile(node, terminalId) {
    tileHosts.set(terminalId, node);
    ensureRenderer(terminalId);
    return {
      destroy() {
        disposeTile(terminalId);
      },
    };
  }

  function ensureRenderer(terminalId) {
    if (
      !mounted ||
      !tileHosts.has(terminalId) ||
      tileRegistry.has(terminalId) ||
      rendererPromises.has(terminalId)
    ) {
      return;
    }
    const loading = initializeTerminal(terminalId)
      .catch(() => {
        if (mounted) {
          viewState.streams = {
            ...viewState.streams,
            [terminalId]: {
              status: TERMINAL_STREAM_ERROR,
              error: t(
                'terminals.rendererError',
                'The browser terminal renderer could not be loaded.',
              ),
              errorCode: '',
            },
          };
        }
      })
      .finally(() => {
        if (rendererPromises.get(terminalId) === loading) {
          rendererPromises.delete(terminalId);
        }
      });
    rendererPromises.set(terminalId, loading);
  }

  function disposeTile(terminalId) {
    tileHosts.delete(terminalId);
    rendererPromises.delete(terminalId);
    pendingSnapshots.delete(terminalId);
    pendingOutputs.delete(terminalId);
    const tile = tileRegistry.get(terminalId);
    if (!tile) {
      return;
    }
    tileRegistry.delete(terminalId);
    fittedGrids = Object.fromEntries(
      Object.entries(fittedGrids).filter(([id]) => id !== terminalId),
    );
    tile.resizeObserver?.disconnect();
    tile.inputDisposable?.dispose();
    tile.scrollDisposable?.dispose();
    tile.xterm?.dispose();
  }

  function loadXtermModules() {
    if (!xtermModulesPromise) {
      xtermModulesPromise = Promise.all([
        import('@xterm/xterm'),
        import('@xterm/addon-fit'),
      ]);
    }
    return xtermModulesPromise;
  }

  async function initializeTerminal(terminalId) {
    const [{ Terminal }, { FitAddon }] = await loadXtermModules();
    const host = tileHosts.get(terminalId);
    if (!mounted || !host) {
      return;
    }
    const interactive =
      !terminalIsFinished(findTerminal(terminalId)) && !serverUnavailable;
    const xtermInstance = new Terminal({
      allowTransparency: false,
      convertEol: false,
      cursorBlink: interactive,
      cursorInactiveStyle: 'outline',
      disableStdin: !interactive,
      fontFamily: cssToken('--font-mono', 'IBM Plex Mono, monospace'),
      fontSize: TERMINAL_BASE_FONT_SIZE,
      lineHeight: 1,
      minimumContrastRatio: 4.5,
      rightClickSelectsWord: true,
      scrollback: 2_000,
      scrollOnUserInput: true,
      smoothScrollDuration: 100,
      theme: terminalTheme(),
    });
    const fitAddonInstance = new FitAddon();
    xtermInstance.loadAddon(fitAddonInstance);
    xtermInstance.open(host);
    try {
      fitAddonInstance.fit();
    } catch {
      // The host may not have settled yet; scheduleFit will retry.
    }
    const inputDisposable = xtermInstance.onData((data) => {
      if (!terminalIsFinished(findTerminal(terminalId))) {
        controller.queueInput(data, { terminalId });
      }
    });
    const scrollDisposable = xtermInstance.onScroll(() => {
      scrolledBackByTerminal[terminalId] =
        xtermInstance.buffer.active.viewportY <
        xtermInstance.buffer.active.baseY;
    });
    let resizeObserverInstance = null;
    if (typeof globalThis.ResizeObserver === 'function') {
      resizeObserverInstance = new ResizeObserver(() =>
        scheduleFit(terminalId),
      );
      resizeObserverInstance.observe(host);
    }
    tileRegistry.set(terminalId, {
      xterm: xtermInstance,
      fitAddon: fitAddonInstance,
      resizeObserver: resizeObserverInstance,
      inputDisposable,
      scrollDisposable,
      lastFitCols: null,
      lastFitRows: null,
      fitFollowUpScheduled: false,
      writeInFlight: false,
      snapshotGeneration: 0,
    });
    const pending = pendingSnapshots.get(terminalId);
    if (pending) {
      pendingSnapshots.delete(terminalId);
      writeSnapshot(terminalId, pending.ansi, pending.terminal);
    }
    const queued = pendingOutputs.get(terminalId);
    if (queued) {
      pendingOutputs.delete(terminalId);
      for (const data of queued) {
        xtermInstance.write(data);
      }
    }
    scheduleFit(terminalId);
    if (pendingFocusTerminalId === terminalId) {
      pendingFocusTerminalId = '';
      xtermInstance.focus();
    }
  }

  function writeSnapshot(terminalId, ansi, snapshotTerminal) {
    const tile = tileRegistry.get(terminalId);
    if (!tile?.xterm) {
      return;
    }
    const columns = snapshotTerminal?.columns;
    const rows = snapshotTerminal?.rows;
    if (
      Number.isInteger(columns) &&
      Number.isInteger(rows) &&
      columns > 0 &&
      rows > 0 &&
      (tile.xterm.cols !== columns || tile.xterm.rows !== rows)
    ) {
      tile.xterm.resize(columns, rows);
    }
    tile.writeInFlight = true;
    tile.snapshotGeneration += 1;
    const generation = tile.snapshotGeneration;
    tile.xterm.reset();
    scrolledBackByTerminal[terminalId] = false;
    try {
      tile.xterm.write(ansi, () => {
        if (
          tileRegistry.get(terminalId) !== tile ||
          tile.snapshotGeneration !== generation
        ) {
          return;
        }
        tile.writeInFlight = false;
        scheduleFit(terminalId);
      });
    } catch {
      tile.writeInFlight = false;
      scheduleFit(terminalId);
    }
  }

  function scheduleFit(terminalId) {
    queueMicrotask(() => fitTerminal(terminalId));
  }

  // A one-shot layout change (tab remount, maximize) measures a transient
  // geometry; re-fitting once on the next animation frame gives the grid a
  // chance to settle and lets the controller's stability pass see the
  // confirmed size as a genuinely separated second measurement.
  function scheduleFitFollowUp(terminalId) {
    const tile = tileRegistry.get(terminalId);
    if (!tile || tile.fitFollowUpScheduled) {
      return;
    }
    tile.fitFollowUpScheduled = true;
    requestAnimationFrame(() => {
      const current = tileRegistry.get(terminalId);
      if (current) {
        current.fitFollowUpScheduled = false;
      }
      fitTerminal(terminalId, { fromFollowUp: true });
    });
  }

  function fitTerminal(terminalId, { fromFollowUp = false } = {}) {
    const tile = tileRegistry.get(terminalId);
    const host = tileHosts.get(terminalId);
    if (
      !tile?.xterm ||
      !tile.fitAddon ||
      !host ||
      tile.writeInFlight ||
      host.clientWidth <= 0 ||
      host.clientHeight <= 0
    ) {
      return;
    }
    try {
      tile.fitAddon.fit();
      // Increase the font size until the grid fits within the PTY dimension
      // limits. Font metrics are not perfectly proportional, so this may
      // need more than one pass.
      let attempts = 0;
      while (
        (tile.xterm.cols > TERMINAL_MAX_COLUMNS ||
          tile.xterm.rows > TERMINAL_MAX_ROWS) &&
        attempts < 5
      ) {
        const colScale = tile.xterm.cols / TERMINAL_MAX_COLUMNS;
        const rowScale = tile.xterm.rows / TERMINAL_MAX_ROWS;
        tile.xterm.options.fontSize = Math.ceil(
          tile.xterm.options.fontSize * Math.max(colScale, rowScale),
        );
        tile.fitAddon.fit();
        attempts += 1;
      }
      // Final safety net: clamp into the server's accepted bounds so a tiny
      // host produces a legal minimum grid instead of a rejected resize.
      const fitted = clampTerminalGrid(tile.xterm.cols, tile.xterm.rows);
      if (
        fitted.columns !== tile.xterm.cols ||
        fitted.rows !== tile.xterm.rows
      ) {
        tile.xterm.resize(fitted.columns, fitted.rows);
      }
      fittedGrids = {
        ...fittedGrids,
        [terminalId]: { columns: fitted.columns, rows: fitted.rows },
      };
      controller.resize(
        fitted.columns,
        fitted.rows,
        terminalId,
        maximizedTerminalId === terminalId,
      );
      const geometryChanged =
        tile.lastFitCols !== fitted.columns || tile.lastFitRows !== fitted.rows;
      tile.lastFitCols = fitted.columns;
      tile.lastFitRows = fitted.rows;
      if (geometryChanged && !fromFollowUp) {
        scheduleFitFollowUp(terminalId);
      }
    } catch {
      // The host may be between layout states while the view is mounting.
    }
  }

  function scrollToLatest(terminalId) {
    const tile = tileRegistry.get(terminalId);
    tile?.xterm?.scrollToBottom();
    scrolledBackByTerminal[terminalId] = false;
    if (!terminalIsFinished(findTerminal(terminalId))) {
      tile?.xterm?.focus();
    }
  }

  async function toggleMaximize(terminalId) {
    if (maximizedTerminalId === terminalId) {
      maximizedTerminalId = '';
    } else {
      maximizedTerminalId = terminalId;
      controller.selectTerminal(terminalId);
    }
    await tick();
    for (const id of tileRegistry.keys()) {
      scheduleFit(id);
    }
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
    const tile = tileRegistry.get(terminalId);
    if (tile?.xterm) {
      tile.xterm.focus();
    } else {
      pendingFocusTerminalId = terminalId;
    }
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
      title: t('terminals.closedTitle', 'Terminal closed'),
      message: wasRunning
        ? t(
            'terminals.closedMessage',
            'The Terminal Session was removed from the list and is being stopped.',
          )
        : t(
            'terminals.closedMessageHistory',
            'The Terminal Session was removed from the list.',
          ),
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

  function openStartDialog() {
    applyLaunchHistory(viewState.launchHistory[0] ?? null);
    startName = '';
    startGroupId = groupCanEdit(selectedGroup)
      ? (viewState.selectedGroupId ?? '')
      : '';
    viewState.startError = '';
    startDialogOpen = true;
  }

  function applyLaunchHistory(entry) {
    selectedLaunchHistoryId = entry?.id ?? '';
    startCommand = entry?.command ?? '';
    startArguments = Array.isArray(entry?.args) ? entry.args.join('\n') : '';
    startWorkdir = entry?.workdir ?? '';
    viewState.startError = '';
  }

  function selectLaunchHistory(entryId) {
    const entry = viewState.launchHistory.find((item) => item.id === entryId);
    if (entry) {
      applyLaunchHistory(entry);
    }
  }

  function markLaunchHistoryEdited() {
    selectedLaunchHistoryId = '';
    viewState.startError = '';
  }

  function launchHistoryLabel(entry) {
    const command =
      String(entry?.command || '').trim() ||
      t('terminals.commandPlaceholder', 'Default shell');
    const argumentsList = Array.isArray(entry?.args) ? entry.args : [];
    return [command, ...argumentsList].join(' ');
  }

  function launchHistoryWorkdir(entry) {
    return (
      String(entry?.workdir || '').trim() ||
      t('terminals.workdirPlaceholder', 'User home directory')
    );
  }

  function closeStartDialog() {
    if (viewState.startingTerminal) {
      return;
    }
    startDialogOpen = false;
    viewState.startError = '';
  }

  async function submitStartTerminal(event) {
    event.preventDefault();
    const command = startCommand.trim();
    const workdir = startWorkdir.trim();
    const name = startName.trim();
    const args = startArguments
      .split(/\r?\n/)
      .filter((argument) => argument.length > 0);
    const params = {};
    if (command) {
      params.command = command;
    }
    if (args.length) {
      params.args = args;
    }
    if (workdir) {
      params.workdir = workdir;
    }
    if (name) {
      params.name = name;
    }

    const started = await controller.startManualTerminal(params, {
      groupId: startGroupId,
    });
    if (!started) {
      return;
    }
    startDialogOpen = false;
    await tick();
    activateTerminal(started.terminal_id);
    onToast({
      title: t('terminals.startedTitle', 'Terminal started'),
      message: t(
        'terminals.startedMessage',
        'The manual Terminal Session is live and ready for input.',
      ),
      variant: 'success',
    });
  }

  function groupKindLabel(kind) {
    if (kind === 'user') {
      return t('terminals.kind.user', 'My group');
    }
    if (kind === 'agent') {
      return t('terminals.kind.agent', 'Agent');
    }
    if (kind === 'finished') {
      return t('terminals.kind.finished', 'Finished');
    }
    return t('terminals.kind.manual', 'Manual');
  }

  function groupCanEdit(group) {
    return group?.kind === 'user' || group?.kind === 'agent';
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

  function openCreateGroupDialog() {
    groupDialogMode = 'create';
    groupDialogName = '';
    groupDialogTargetId = '';
    viewState.actionError = '';
    groupDialogOpen = true;
  }

  function openRenameGroupDialog(group) {
    if (!groupCanEdit(group)) {
      return;
    }
    groupDialogMode = 'rename';
    groupDialogName = group.name;
    groupDialogTargetId = group.group_id;
    viewState.actionError = '';
    groupDialogOpen = true;
  }

  function closeGroupDialog() {
    if (viewState.groupActionPending) {
      return;
    }
    groupDialogOpen = false;
    viewState.actionError = '';
  }

  async function submitGroupDialog(event) {
    event.preventDefault();
    const name = groupDialogName.trim();
    if (!name) {
      viewState.actionError = t(
        'terminals.groupNameRequired',
        'Enter a group name.',
      );
      return;
    }
    if (groupDialogMode === 'create') {
      const group = await controller.createGroup(name);
      if (!group) {
        return;
      }
      groupDialogOpen = false;
      onToast({
        title: t('terminals.groupCreatedTitle', 'Group created'),
        message: t(
          'terminals.groupCreatedMessage',
          'The group is ready and appears in the terminal list.',
        ),
        variant: 'success',
      });
    } else {
      const renamed = await controller.renameGroup(groupDialogTargetId, name);
      if (!renamed) {
        return;
      }
      groupDialogOpen = false;
      onToast({
        title: t('terminals.groupRenamedTitle', 'Group renamed'),
        message: t(
          'terminals.groupRenamedMessage',
          'The group name was updated.',
        ),
        variant: 'success',
      });
    }
  }

  function openDeleteGroupDialog(group) {
    if (!groupCanEdit(group)) {
      return;
    }
    deleteGroupTargetId = group.group_id;
    viewState.actionError = '';
    deleteGroupDialogOpen = true;
  }

  function closeDeleteGroupDialog() {
    if (viewState.groupActionPending) {
      return;
    }
    deleteGroupDialogOpen = false;
    viewState.actionError = '';
  }

  async function confirmDeleteGroup() {
    const group = viewState.groups.find(
      (item) => item.group_id === deleteGroupTargetId,
    );
    if (!group) {
      deleteGroupDialogOpen = false;
      return;
    }
    const result = await controller.deleteGroup(deleteGroupTargetId);
    if (!result) {
      return;
    }
    deleteGroupDialogOpen = false;
    onToast({
      title: t('terminals.deleteGroupTitle', 'Delete group'),
      message: t(
        'terminals.deleteGroupMessage',
        'The group was deleted and its terminals were stopped.',
      ),
      variant: 'success',
    });
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

  function terminalTarget(item) {
    if (!item?.owner) {
      return t('terminals.manualOwner', 'Manual');
    }
    const agentId = item?.owner?.agent_id || '—';
    const projectId = item?.owner?.project_id;
    return projectId ? `${agentId}@${projectId}` : agentId;
  }

  function terminalTitle(item) {
    const customName = typeof item?.name === 'string' ? item.name.trim() : '';
    if (customName) {
      return customName;
    }
    const announcedTitle =
      typeof item?.title === 'string' ? item.title.trim() : '';
    if (announcedTitle) {
      return announcedTitle;
    }
    const launched = launchedCommand(item);
    if (launched) {
      return launched;
    }
    const command = String(item?.command || '').trim();
    const executable = command.split(/[\\/]/).pop()?.toLowerCase() || '';
    const labels = {
      'pwsh.exe': 'PowerShell',
      pwsh: 'PowerShell',
      'powershell.exe': 'Windows PowerShell',
      powershell: 'Windows PowerShell',
      'cmd.exe': 'Command Prompt',
      cmd: 'Command Prompt',
      'bash.exe': 'Bash',
      bash: 'Bash',
      zsh: 'Zsh',
      fish: 'Fish',
    };
    return labels[executable] || command || 'Terminal';
  }

  function launchedCommand(item) {
    const launchCommand = String(item?.launch_command || '').trim();
    if (!launchCommand) {
      return '';
    }
    const args = Array.isArray(item?.launch_args) ? item.launch_args : [];
    return [launchCommand, ...args].join(' ');
  }

  function cssToken(name, fallback) {
    if (typeof document === 'undefined') {
      return fallback;
    }
    return (
      getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim() || fallback
    );
  }

  function terminalTheme() {
    return {
      background: cssToken('--terminal-surface', '#0E0D0B'),
      foreground: cssToken('--text-hi', '#EEE7DC'),
      cursor: cssToken('--accent', '#E8870A'),
      cursorAccent: cssToken('--terminal-surface', '#0E0D0B'),
      selectionBackground: cssToken('--border-2', '#5D4A35'),
      black: cssToken('--terminal-surface', '#0E0D0B'),
      red: cssToken('--red', '#FC8181'),
      green: cssToken('--green', '#4ADE80'),
      yellow: cssToken('--amber', '#F59E0B'),
      blue: cssToken('--blue', '#60A5FA'),
      magenta: '#D8A4E2',
      cyan: '#67D4C1',
      white: cssToken('--text-hi', '#EEE7DC'),
      brightBlack: cssToken('--text-lo', '#5E4C38'),
      brightRed: '#FFA0A0',
      brightGreen: '#7AE7A4',
      brightYellow: '#FBC45B',
      brightBlue: '#8EC0FF',
      brightMagenta: '#E7BDEE',
      brightCyan: '#8DE1D2',
      brightWhite: '#FFF9F0',
    };
  }

  function terminalError(message) {
    return message || t('terminals.unknownError', 'Unknown terminal error');
  }

  // Diagnostics: a tile whose settled grid differs from the server's
  // authoritative dimensions renders TUI content at the wrong cell count —
  // stretched or wrapped borders. While a resize correction is still inside
  // the pipeline (stability pass, debounce, in-flight request) the mismatch
  // is expected and stays quiet; a lit hint means the pipeline closed
  // without reconciling the two sizes.
  function gridMismatchHint(terminalId) {
    const fitted = fittedGrids[terminalId];
    const item = findTerminal(terminalId);
    if (!fitted || !item || terminalIsFinished(item) || serverUnavailable) {
      return '';
    }
    if (viewState.streams[terminalId]?.gridPending) {
      return '';
    }
    if (fitted.columns === item.columns && fitted.rows === item.rows) {
      return '';
    }
    return t('terminals.gridMismatch', 'Tile {fitted} · Session {server}', {
      fitted: `${fitted.columns}×${fitted.rows}`,
      server: `${item.columns}×${item.rows}`,
    });
  }
</script>

<svelte:document
  onmousedown={handleGroupMenuDocumentMouseDown}
  onkeydown={handleGroupMenuDocumentKeyDown}
/>
<svelte:window onresize={closeGroupMenu} />

<section class="terminals-view" aria-label={t('terminals.title', 'Terminals')}>
  <header class="terminals-view__toolbar">
    <div
      class="terminals-view__group-tabs"
      aria-label={t('terminals.groupsLabel', 'Groups')}
      onscroll={closeGroupMenu}
    >
      {#each viewState.groups as group (group.group_id)}
        <div
          class="terminals-view__group-tab-wrap"
          class:terminals-view__group-tab-wrap--editable={groupCanEdit(group)}
        >
          <button
            type="button"
            class="terminals-view__group-tab"
            class:active={group.group_id === viewState.selectedGroupId}
            aria-current={group.group_id === viewState.selectedGroupId
              ? 'true'
              : undefined}
            aria-label={`${group.name}: ${t('terminals.count', '{count} terminals', { count: group.terminal_count })}`}
            use:tooltip={group.name}
            onclick={() => controller.selectGroup(group.group_id)}
          >
            <span class="terminals-view__group-tab-label">
              <span class="terminals-view__group-tab-name">{group.name}</span>
              <span class="terminals-view__group-tab-count">
                {group.terminal_count}
              </span>
            </span>
          </button>
          {#if groupCanEdit(group)}
            <span class="terminals-view__group-actions">
              <button
                type="button"
                class="terminals-view__group-action-menu-trigger"
                class:terminals-view__group-action-menu-trigger--open={openGroupMenuId ===
                  group.group_id}
                aria-label={t('terminals.groupActions', 'Group actions')}
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
                      openRenameGroupDialog(group);
                    }}
                  >
                    {t('terminals.renameGroupAction', 'Rename group')}
                  </button>
                  <button
                    type="button"
                    class="terminals-view__group-menu-item terminals-view__group-menu-item--danger"
                    role="menuitem"
                    onclick={() => {
                      closeGroupMenu();
                      openDeleteGroupDialog(group);
                    }}
                  >
                    {t('terminals.deleteGroupAction', 'Delete group')}
                  </button>
                </div>
              {/if}
            </span>
          {/if}
        </div>
      {/each}
      {#if viewState.loading && viewState.groups.length === 0}
        <span class="terminals-view__group-status">
          {t('terminals.loading', 'Loading terminal sessions…')}
        </span>
      {:else if viewState.groups.length === 0}
        <span class="terminals-view__group-status">
          {t('terminals.emptyTitle', 'No terminal sessions')}
        </span>
      {/if}
    </div>
    <div class="terminals-view__toolbar-actions">
      <Button
        variant="secondary"
        disabled={serverUnavailable}
        onClick={openCreateGroupDialog}
      >
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {t('terminals.addGroup', 'Add group')}
      </Button>
      <Button
        variant="primary"
        disabled={serverUnavailable}
        onClick={openStartDialog}
      >
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {t('terminals.new', 'New terminal')}
      </Button>
    </div>
  </header>

  <div class="terminals-view__detail">
    {#if viewState.listError && !serverUnavailable}
      <Banner variant="error" class="terminals-view__feedback">
        <span
          >{t(
            'terminals.listError',
            'Terminal sessions could not be loaded.',
          )}</span
        >
        <Button variant="secondary" onClick={() => controller.loadTerminals()}>
          {t('common.retry', 'Retry')}
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
        aria-label={t('terminals.canvasLabel', 'Terminal canvas')}
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
                  use:tooltip={terminalTitle(item)}>{terminalTitle(item)}</span
                >
                <span
                  class="terminals-view__tile-target"
                  use:tooltip={terminalTarget(item)}
                  >{terminalTarget(item)}</span
                >
                {#if gridMismatch}
                  <span
                    class="terminals-view__grid-mismatch"
                    use:tooltip={t(
                      'terminals.gridMismatchHelp',
                      'This tile shows its content at a different size than the terminal session uses. Try resizing the window or maximizing this tile once to reapply the session size.',
                    )}>{gridMismatch}</span
                  >
                {/if}
                {#if scrolledBackByTerminal[item.terminal_id]}
                  <button
                    type="button"
                    class="terminals-view__latest-action"
                    onclick={(event) => {
                      event.stopPropagation();
                      scrollToLatest(item.terminal_id);
                    }}
                  >
                    {t('terminals.scrollLatest', 'Jump to latest')}
                  </button>
                {/if}
                <span
                  class="terminals-view__tile-actions"
                  role="presentation"
                  onclick={(event) => event.stopPropagation()}
                  ondblclick={(event) => event.stopPropagation()}
                  onkeydown={(event) => event.stopPropagation()}
                >
                  <Button
                    variant="tertiary"
                    icon
                    class="terminals-view__tile-action"
                    ariaLabel={isMaximized
                      ? t('terminals.restore', 'Restore')
                      : t('terminals.maximize', 'Maximize')}
                    tooltip={isMaximized
                      ? t('terminals.restore', 'Restore')
                      : t('terminals.maximize', 'Maximize')}
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
                    ariaLabel={t('terminals.close', 'Close terminal')}
                    tooltip={t('terminals.close', 'Close terminal')}
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
                    {t(
                      'terminals.streamGap',
                      'Terminal output continuity was lost; rebuilding the live screen.',
                    )}
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
                aria-label={t(
                  isFinished
                    ? 'terminals.historyTerminalLabel'
                    : 'terminals.liveTerminalLabel',
                  isFinished
                    ? 'Retained terminal history.'
                    : 'Live terminal. Click to focus and type.',
                )}
                onpointerdown={(event) =>
                  activateTerminalFromPointer(event, item.terminal_id)}
                onkeydown={(event) =>
                  activateTerminalFromKeyboard(event, item.terminal_id)}
              ></div>
            </div>
          </div>
        {/each}
      </div>
    {:else}
      <EmptyState
        fill
        title={selectedGroup
          ? t('terminals.groupEmptyTitle', 'No terminals in this group')
          : t('terminals.detailEmptyTitle', 'Open a terminal')}
        description={selectedGroup
          ? t(
              'terminals.groupEmptyDescription',
              'Start a terminal in this group, or ask an agent to open one here.',
            )
          : t(
              'terminals.detailEmptyDescription',
              'Start the local default shell or choose a command such as codex. Agent terminals will appear here too.',
            )}
      />
    {/if}
  </div>
</section>

{#if startDialogOpen}
  <Modal
    title={t('terminals.startTitle', 'New terminal')}
    labelledById="terminal-start-modal-title"
    class="terminals-view__start-modal"
    closeDisabled={viewState.startingTerminal}
    onClose={closeStartDialog}
  >
    {#snippet body()}
      <form id="terminal-start-form" onsubmit={submitStartTerminal}>
        <div class="modal-body terminals-view__start-form">
          <p class="terminals-view__start-intro">
            {t(
              'terminals.startIntro',
              'Every manual terminal opens the server user’s default shell. A Command is entered into that shell like typed input, so the terminal keeps working after the command ends.',
            )}
          </p>

          {#if viewState.startError && !serverUnavailable}
            <Banner variant="error" role="alert">
              {terminalError(viewState.startError)}
            </Banner>
          {/if}

          {#if viewState.launchHistory.length > 0}
            <FormField
              controlId="terminal-start-history"
              label={t('terminals.historyLabel', 'Recent setup')}
              help={t(
                'terminals.historyHelp',
                'Saved on this vBot server. Choosing a setup fills Command, Arguments, and Working directory.',
              )}
            >
              {#snippet children(field)}
                <Dropdown
                  id={field.controlId}
                  value={selectedLaunchHistoryId}
                  options={launchHistoryOptions}
                  ariaLabel={t('terminals.historyLabel', 'Recent setup')}
                  ariaDescribedby={field.describedBy}
                  disabled={viewState.startingTerminal}
                  triggerClass="terminals-view__history-dropdown"
                  listClass="terminals-view__history-dropdown-list"
                  onValueChange={selectLaunchHistory}
                />
              {/snippet}
            </FormField>
          {/if}

          <FormField
            controlId="terminal-start-command"
            label={t('terminals.commandLabel', 'Command')}
            help={t(
              'terminals.commandHelp',
              'Optional. For example: codex, powershell, bash, or python.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={startCommand}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.commandPlaceholder', 'Default shell')}
                onInput={(next) => {
                  startCommand = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField
            controlId="terminal-start-name"
            label={t('terminals.nameLabel', 'Name')}
            help={t(
              'terminals.nameHelp',
              'Optional. A label so you and the agent can talk about this terminal, for example joe.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={startName}
                disabled={viewState.startingTerminal}
                placeholder={t('terminals.namePlaceholder', 'Unnamed')}
                onInput={(next) => {
                  startName = next;
                }}
              />
            {/snippet}
          </FormField>

          <FormField
            controlId="terminal-start-arguments"
            label={t('terminals.argumentsLabel', 'Arguments')}
            help={t(
              'terminals.argumentsHelp',
              'Optional. Enter one exact argument per line; spaces within a line are preserved.',
            )}
          >
            {#snippet children(field)}
              <TextArea
                id={field.controlId}
                code
                rows={4}
                aria-describedby={field.describedBy}
                value={startArguments}
                disabled={viewState.startingTerminal}
                placeholder={t(
                  'terminals.argumentsPlaceholder',
                  '--profile\nwork',
                )}
                onInput={(next) => {
                  startArguments = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>

          <FormField
            controlId="terminal-start-group"
            label={t('terminals.startGroupLabel', 'Group')}
            help={t(
              'terminals.startGroupHelp',
              'Optional. Start the terminal inside a group. Automatic groups are created for each agent and for manual terminals.',
            )}
          >
            {#snippet children(field)}
              <Dropdown
                id={field.controlId}
                value={startGroupId}
                options={[groupOptionAutomatic, ...groupOptions]}
                ariaLabel={t('terminals.startGroupLabel', 'Group')}
                ariaDescribedby={field.describedBy}
                disabled={viewState.startingTerminal}
                triggerClass="terminals-view__group-dropdown"
                listClass="terminals-view__group-dropdown-list"
                onValueChange={(next) => {
                  startGroupId = next;
                }}
              />
            {/snippet}
          </FormField>

          <FormField
            controlId="terminal-start-workdir"
            label={t('terminals.workdirLabel', 'Working directory')}
            help={t(
              'terminals.workdirHelp',
              'Optional. Defaults to the server user’s home directory.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={startWorkdir}
                disabled={viewState.startingTerminal}
                placeholder={t(
                  'terminals.workdirPlaceholder',
                  'User home directory',
                )}
                onInput={(next) => {
                  startWorkdir = next;
                  markLaunchHistoryEdited();
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={viewState.startingTerminal}
        onClick={closeStartDialog}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-start-form"
        variant="primary"
        loading={viewState.startingTerminal}
      >
        {t('terminals.start', 'Start terminal')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if groupDialogOpen}
  <Modal
    title={groupDialogMode === 'create'
      ? t('terminals.createGroupTitle', 'New group')
      : t('terminals.renameGroupTitle', 'Rename group')}
    labelledById="terminal-group-modal-title"
    class="terminals-view__group-modal"
    closeDisabled={viewState.groupActionPending}
    onClose={closeGroupDialog}
  >
    {#snippet body()}
      <form id="terminal-group-form" onsubmit={submitGroupDialog}>
        <div class="modal-body">
          {#if viewState.actionError && !serverUnavailable}
            <Banner variant="error" role="alert">
              {terminalError(viewState.actionError)}
            </Banner>
          {/if}
          <FormField
            controlId="terminal-group-name"
            label={t('terminals.groupNameLabel', 'Group name')}
            help={t(
              'terminals.groupNameHelp',
              'Shown in the sidebar. Terminals you start here join this group.',
            )}
          >
            {#snippet children(field)}
              <TextField
                id={field.controlId}
                variant="modal"
                aria-describedby={field.describedBy}
                value={groupDialogName}
                disabled={viewState.groupActionPending}
                placeholder={t('terminals.groupNamePlaceholder', 'e.g. Work')}
                onInput={(next) => {
                  groupDialogName = next;
                  viewState.actionError = '';
                }}
              />
            {/snippet}
          </FormField>
        </div>
      </form>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={viewState.groupActionPending}
        onClick={closeGroupDialog}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        type="submit"
        form="terminal-group-form"
        variant="primary"
        loading={viewState.groupActionPending}
      >
        {groupDialogMode === 'create'
          ? t('terminals.createGroup', 'Create group')
          : t('terminals.renameGroup', 'Rename')}
      </Button>
    {/snippet}
  </Modal>
{/if}

{#if deleteGroupDialogOpen}
  {@const deleteGroup = viewState.groups.find(
    (group) => group.group_id === deleteGroupTargetId,
  )}
  <Modal
    title={t('terminals.deleteGroupTitle', 'Delete group')}
    labelledById="terminal-delete-group-title"
    closeDisabled={viewState.groupActionPending}
    onClose={closeDeleteGroupDialog}
  >
    {#snippet body()}
      <div class="modal-body">
        {#if viewState.actionError && !serverUnavailable}
          <Banner variant="error" role="alert">
            {terminalError(viewState.actionError)}
          </Banner>
        {/if}
        <p class="terminals-view__delete-intro">
          {t(
            'terminals.deleteGroupWarning',
            'Deleting this group stops every running terminal in it. Finished terminals remain available until they expire.',
          )}
        </p>
        {#if deleteGroup && deleteGroup.terminal_count > 0}
          <Banner variant="warn">
            {t(
              'terminals.deleteGroupCount',
              '{count} terminals are in this group.',
              {
                count: deleteGroup.terminal_count,
              },
            )}
          </Banner>
        {/if}
      </div>
    {/snippet}
    {#snippet footer()}
      <Button
        variant="secondary"
        disabled={viewState.groupActionPending}
        onClick={closeDeleteGroupDialog}
      >
        {t('common.cancel', 'Cancel')}
      </Button>
      <Button
        variant="danger"
        loading={viewState.groupActionPending}
        onClick={() => void confirmDeleteGroup()}
      >
        {deleteGroup?.terminal_count
          ? t('terminals.deleteGroupConfirm', 'Delete {count} terminal(s)', {
              count: deleteGroup.terminal_count,
            })
          : t('terminals.deleteGroupEmptyConfirm', 'Delete group')}
      </Button>
    {/snippet}
  </Modal>
{/if}

<style>
  .terminals-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  .terminals-view__toolbar {
    display: flex;
    min-width: 0;
    min-height: 50px;
    align-items: stretch;
    gap: var(--space-sm);
    padding: 0 var(--space-lg);
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .terminals-view__group-tabs {
    display: flex;
    min-width: 0;
    flex: 1;
    align-items: stretch;
    gap: 2px;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .terminals-view__group-tabs::-webkit-scrollbar {
    display: none;
  }

  .terminals-view__group-tab-wrap {
    position: relative;
    display: flex;
    min-width: 0;
    flex: 0 0 auto;
  }

  .terminals-view__group-tab {
    display: flex;
    min-width: 0;
    align-items: center;
    padding: 0 14px;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--text-lo);
    background: transparent;
    font-family: var(--font-ui);
    font-size: var(--fs-label-md);
    font-weight: 500;
    white-space: nowrap;
    transition:
      border-color 150ms ease,
      color 150ms ease;
  }

  .terminals-view__group-tab-label {
    display: flex;
    min-width: 0;
    align-items: baseline;
    gap: 9px;
  }

  .terminals-view__group-tab:hover,
  .terminals-view__group-tab:focus-visible {
    color: var(--text-med);
    outline: none;
  }

  .terminals-view__group-tab.active {
    border-bottom-color: var(--accent);
    color: var(--accent);
  }

  .terminals-view__group-tab-wrap--editable .terminals-view__group-tab {
    padding-right: 30px;
  }

  .terminals-view__group-tab-name {
    min-width: 0;
    max-width: 180px;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .terminals-view__group-tab-count {
    flex: 0 0 auto;
    font-family: var(--font-mono);
    font-size: var(--fs-mono-sm);
    color: var(--text-lo);
  }

  .terminals-view__group-actions {
    position: absolute;
    z-index: 1;
    top: 50%;
    right: 4px;
    display: flex;
    align-items: center;
    transform: translateY(-50%);
  }

  .terminals-view__group-action-menu-trigger {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    padding: 0;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-med);
    opacity: 0;
    pointer-events: none;
    cursor: pointer;
    transition:
      background 150ms ease,
      color 150ms ease,
      opacity 150ms ease;
  }

  .terminals-view__group-action-menu-trigger svg {
    width: 12px;
    height: 12px;
    fill: currentColor;
  }

  .terminals-view__group-tab-wrap:hover
    .terminals-view__group-action-menu-trigger,
  .terminals-view__group-tab-wrap:focus-within
    .terminals-view__group-action-menu-trigger,
  .terminals-view__group-action-menu-trigger--open {
    opacity: 1;
    pointer-events: auto;
  }

  .terminals-view__group-action-menu-trigger:hover,
  .terminals-view__group-action-menu-trigger--open {
    background: var(--surface-3);
    color: var(--text-hi);
  }

  .terminals-view__group-action-menu-trigger:focus-visible {
    outline: 1px solid var(--accent);
    outline-offset: 1px;
  }

  .terminals-view__group-menu {
    position: fixed;
    z-index: var(--z-floating);
    width: max-content;
    min-width: 132px;
    max-width: calc(100vw - 16px);
    overflow-y: auto;
    padding: 4px;
    border: 1px solid var(--border);
    border-radius: var(--r-md);
    background: var(--surface-3);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
  }

  .terminals-view__group-menu-item {
    display: block;
    width: 100%;
    padding: 7px 9px;
    border: 0;
    border-radius: var(--r-sm);
    background: transparent;
    color: var(--text-hi);
    font-family: var(--font-ui);
    font-size: var(--fs-body-sm);
    text-align: left;
    cursor: pointer;
    transition: background 150ms ease;
  }

  .terminals-view__group-menu-item:hover,
  .terminals-view__group-menu-item:focus-visible {
    outline: none;
    background: var(--accent-12);
  }

  .terminals-view__group-menu-item--danger {
    color: var(--red);
  }

  .terminals-view__group-menu-item--danger:hover,
  .terminals-view__group-menu-item--danger:focus-visible {
    background: rgba(252, 129, 129, 0.14);
  }

  .terminals-view__group-status {
    display: flex;
    align-items: center;
    padding: 0 14px;
    color: var(--text-lo);
    font-size: var(--fs-body-sm);
    white-space: nowrap;
  }

  .terminals-view__toolbar-actions {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: var(--space-sm);
  }

  .terminals-view__detail {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    height: 100%;
    flex-direction: column;
    overflow: hidden;
    padding: 20px;
  }

  :global(.terminals-view__feedback) {
    margin-bottom: 8px;
  }

  .terminals-view__canvas {
    display: grid;
    min-width: 0;
    min-height: 0;
    flex: 1;
    gap: 10px;
  }

  .terminals-view__tile {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: var(--r-lg);
    background: var(--bg);
  }

  .terminals-view__tile--focused {
    border-color: var(--accent-40);
  }

  .terminals-view__tile--hidden {
    display: none;
  }

  .terminals-view__tile--dragging {
    opacity: 0.45;
  }

  .terminals-view__tile--drop-target {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent) inset;
  }

  .terminals-view__tile-bar {
    display: flex;
    min-width: 0;
    padding: 4px 6px 4px 10px;
    border-bottom: 1px solid var(--border);
    color: var(--text-lo);
    background: var(--surface);
    font-family: var(--font-mono);
    font-size: var(--fs-mono-xs);
    letter-spacing: 0.04em;
    cursor: pointer;
  }

  .terminals-view__tile-bar[draggable='true'] {
    cursor: grab;
  }

  .terminals-view__tile-bar[draggable='true']:active {
    cursor: grabbing;
  }

  .terminals-view__tile-bar-primary {
    display: flex;
    min-width: 0;
    width: 100%;
    align-items: center;
    gap: 8px;
  }

  .terminals-view__tile-title {
    min-width: 0;
    overflow: hidden;
    color: var(--text-hi);
    font-weight: 500;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminals-view__tile-target {
    min-width: 0;
    overflow: hidden;
    color: var(--text-med);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminals-view__grid-mismatch {
    flex: 0 0 auto;
    padding: 1px 6px;
    border-radius: 999px;
    color: var(--amber);
    background: var(--accent-dim);
    font-size: var(--fs-mono-xs);
    white-space: nowrap;
    cursor: help;
  }

  .terminals-view__tile-actions {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 4px;
    margin-left: auto;
  }

  :global(.btn-tertiary.btn-icon.terminals-view__tile-action),
  :global(.btn-danger.btn-icon.terminals-view__tile-action) {
    width: 28px;
    height: 28px;
  }

  .terminals-view__latest-action {
    flex: 0 0 auto;
    padding: 0;
    border: 0;
    color: var(--accent);
    background: transparent;
    font: inherit;
  }

  .terminals-view__latest-action:hover,
  .terminals-view__latest-action:focus-visible {
    color: var(--text-hi);
    outline: none;
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  :global(.terminals-view__tile-feedback) {
    margin: 8px 8px 0;
  }

  .terminals-view__tile-chrome {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    flex-direction: column;
    overflow: hidden;
    background: var(--terminal-surface);
  }

  .terminals-view__tile-host {
    min-width: 0;
    min-height: 0;
    flex: 1;
    padding: 8px 10px;
    overflow: hidden;
    background: var(--terminal-surface);
  }

  .terminals-view__tile-host :global(.xterm) {
    height: 100%;
  }

  .terminals-view__tile-host :global(.xterm-viewport),
  .terminals-view__tile-host :global(.xterm-screen) {
    border-radius: var(--r-sm);
  }

  .terminals-view__tile-host
    :global(.xterm-scrollable-element > .scrollbar.vertical) {
    background: var(--terminal-surface);
  }

  .terminals-view__tile-host
    :global(.xterm-scrollable-element > .scrollbar.vertical > .slider) {
    border: 2px solid var(--terminal-surface);
    border-radius: var(--r-sm);
    background: var(--text-lo);
  }

  :global(.modal.terminals-view__start-modal) {
    width: 520px;
  }

  :global(.modal.terminals-view__group-modal) {
    width: 440px;
  }

  .terminals-view__delete-intro {
    margin: 0 0 12px;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }

  :global(.terminals-view__group-dropdown) {
    width: 100%;
  }

  :global(.terminals-view__group-dropdown .dropdown-primitive__trigger) {
    width: 100%;
  }

  :global(.terminals-view__group-dropdown-list) {
    font-family: var(--font-mono);
  }

  .terminals-view__start-form {
    max-height: min(640px, 70vh);
    overflow-y: auto;
  }

  .terminals-view__start-intro {
    margin: 0;
    color: var(--text-med);
    font-size: var(--fs-body-sm);
    line-height: 1.5;
  }

  :global(.terminals-view__history-dropdown) {
    width: 100%;
  }

  :global(.terminals-view__history-dropdown .dropdown-primitive__trigger) {
    width: 100%;
  }

  :global(.terminals-view__history-dropdown-list) {
    font-family: var(--font-mono);
  }

  @media (max-width: 960px) {
    .terminals-view__detail {
      padding: 16px;
    }
  }

  @media (max-width: 640px) {
    .terminals-view {
      overflow: auto;
    }

    .terminals-view__toolbar {
      min-height: auto;
      flex-wrap: wrap;
      padding: var(--space-sm) var(--space-md);
    }

    .terminals-view__group-tabs {
      order: 2;
      width: 100%;
      height: 40px;
      flex-basis: 100%;
    }

    .terminals-view__toolbar-actions {
      width: 100%;
      justify-content: flex-end;
    }

    .terminals-view__group-action-menu-trigger {
      width: 28px;
      height: 28px;
    }

    .terminals-view__group-tab-wrap--editable .terminals-view__group-tab {
      padding-right: 36px;
    }

    .terminals-view__detail {
      min-height: 620px;
      overflow: visible;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .terminals-view__tile-host :global(.xterm-viewport) {
      scroll-behavior: auto;
    }
  }
</style>
