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
    TERMINAL_STREAM_ERROR,
    TERMINAL_STREAM_IDLE,
    createTerminalsController,
    createTerminalsViewState,
    layoutForCount,
    terminalIsFinished,
  } from '$lib/terminalsView.js';
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
  let closingTerminalId = $state('');
  let startDialogOpen = $state(false);
  let selectedLaunchHistoryId = $state('');
  let startCommand = $state('');
  let startArguments = $state('');
  let startWorkdir = $state('');
  let startName = $state('');
  let mounted = false;
  let xtermModulesPromise = null;
  const tileRegistry = new SvelteMap();
  const tileHosts = new SvelteMap();
  const rendererPromises = new SvelteMap();
  const pendingSnapshots = new SvelteMap();
  const pendingOutputs = new SvelteMap();
  const TERMINAL_BASE_FONT_SIZE = 12;
  const TERMINAL_MAX_COLUMNS = 240;
  const TERMINAL_MAX_ROWS = 80;

  let hasTerminals = $derived(viewState.terminals.length > 0);
  let layout = $derived(
    maximizedTerminalId
      ? layoutForCount(1)
      : layoutForCount(viewState.terminals.length),
  );
  let launchHistoryOptions = $derived(
    viewState.launchHistory.map((entry) => ({
      value: entry.id,
      label: launchHistoryLabel(entry),
      secondaryLabel: launchHistoryWorkdir(entry),
    })),
  );

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
      !viewState.terminals.some(
        (item) => item.terminal_id === maximizedTerminalId,
      )
    ) {
      maximizedTerminalId = '';
    }
  });

  $effect(() => {
    for (const item of viewState.terminals) {
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
        import('@xterm/addon-webgl'),
      ]);
    }
    return xtermModulesPromise;
  }

  let WebglAddonClass = null;

  async function initializeTerminal(terminalId) {
    const [{ Terminal }, { FitAddon }, { WebglAddon }] =
      await loadXtermModules();
    WebglAddonClass = WebglAddon;
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
    // Fit to host dimensions so the terminal starts at the right size.
    try {
      fitAddonInstance.fit();
    } catch {
      // The host may not have settled yet; scheduleFit will retry.
    }
    // WebGL is loaded AFTER the first fitTerminal completes — loading it
    // before the snapshot write + resize cycle causes white row gaps.
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
      webglLoaded: false,
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
    tile.xterm.reset();
    scrolledBackByTerminal[terminalId] = false;
    tile.xterm.write(ansi, () => {
      tileRegistry.get(terminalId)?.xterm?.refresh(0, tile.xterm.rows - 1);
      scheduleFit(terminalId);
    });
  }

  function enableWebglRenderer(xtermInstance, WebglAddon) {
    try {
      const webglAddon = new WebglAddon();
      webglAddon.onContextLoss(() => webglAddon.dispose());
      xtermInstance.loadAddon(webglAddon);
    } catch {
      // The built-in DOM renderer remains the safe fallback when WebGL is absent.
    }
  }

  function scheduleFit(terminalId) {
    queueMicrotask(() => fitTerminal(terminalId));
  }

  function fitTerminal(terminalId) {
    const tile = tileRegistry.get(terminalId);
    const host = tileHosts.get(terminalId);
    if (
      !tile?.xterm ||
      !tile.fitAddon ||
      !host ||
      host.clientWidth <= 0 ||
      host.clientHeight <= 0
    ) {
      return;
    }
    try {
      tile.xterm.options.fontSize = TERMINAL_BASE_FONT_SIZE;
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
      // Hard clamp as a final safety net so the backend never rejects.
      const clampedCols = Math.min(tile.xterm.cols, TERMINAL_MAX_COLUMNS);
      const clampedRows = Math.min(tile.xterm.rows, TERMINAL_MAX_ROWS);
      if (clampedCols !== tile.xterm.cols || clampedRows !== tile.xterm.rows) {
        tile.xterm.resize(clampedCols, clampedRows);
      }
      const immediate = maximizedTerminalId === terminalId;
      controller.resize(
        tile.xterm.cols,
        tile.xterm.rows,
        terminalId,
        immediate,
      );
      // Load WebGL after the terminal has settled at its final dimensions
      // and the browser has painted at least once. Creating the WebGL canvas
      // in a microtask (before the first paint) produces white row gaps that
      // only disappear after a subsequent resize triggers a re-render.
      // The first tile in a multi-tile grid is especially prone to this:
      // its host is measured before the grid has fully settled on remount
      // (tab switch), so a single rAF still sees a transient size.
      // We use double rAF to wait for layout + paint, and retry if the
      // host is still 0 (grid not yet flushed) — otherwise the tile would
      // stay on the DOM renderer and keep the TUI gaps for all tiles.
      if (!tile.webglLoaded && WebglAddonClass) {
        const scheduleWebgl = () => {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              const current = tileRegistry.get(terminalId);
              const currentHost = tileHosts.get(terminalId);
              if (!current?.xterm || !WebglAddonClass || current.webglLoaded) {
                return;
              }
              if (
                !currentHost ||
                currentHost.clientWidth <= 0 ||
                currentHost.clientHeight <= 0
              ) {
                scheduleWebgl();
                return;
              }
              current.webglLoaded = true;
              enableWebglRenderer(current.xterm, WebglAddonClass);
              current.xterm.refresh(0, current.xterm.rows - 1);
            });
          });
        };
        scheduleWebgl();
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

  // Close one tile with a single click: a running terminal is stopped first
  // (kill), then removed from the retained catalog (forget). A finished
  // terminal is only forgotten. No confirmation — the X is the intent.
  async function closeTerminal(terminalId) {
    const item = findTerminal(terminalId);
    if (!item || closingTerminalId === terminalId) {
      return;
    }
    closingTerminalId = terminalId;
    try {
      if (!terminalIsFinished(item)) {
        const stopped = await controller.killTerminal(terminalId);
        if (!stopped) {
          return;
        }
        const afterKill = findTerminal(terminalId);
        if (!afterKill) {
          // The post-kill reload already removed the session — nothing left
          // to forget.
          return;
        }
      }
      const dismissed = await controller.forgetTerminal(terminalId);
      if (!dismissed) {
        return;
      }
      const wasRunning = !terminalIsFinished(item);
      onToast({
        title: t('terminals.closedTitle', 'Terminal closed'),
        message: wasRunning
          ? t(
              'terminals.closedMessage',
              'The Terminal Session was stopped and removed from the list.',
            )
          : t(
              'terminals.closedMessageHistory',
              'The Terminal Session was removed from the list.',
            ),
        variant: 'success',
      });
    } finally {
      if (closingTerminalId === terminalId) {
        closingTerminalId = '';
      }
    }
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

    const started = await controller.startManualTerminal(params);
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

  function terminalSession(item) {
    return (
      item?.owner?.session_id || t('terminals.localOperator', 'Local operator')
    );
  }

  function shortSession(item) {
    const sessionId = terminalSession(item);
    return sessionId.length > 14 ? `${sessionId.slice(0, 12)}…` : sessionId;
  }

  function stateLabel(state) {
    return t(`terminals.state.${state}`, state || 'Unknown');
  }

  function startedAt(item) {
    const date = new Date(item?.started_at || '');
    if (Number.isNaN(date.getTime())) {
      return '—';
    }
    return new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(date);
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
</script>

<section class="terminals-view" aria-labelledby="terminals-title">
  <aside
    class="terminals-view__list-pane secondary-pane"
    aria-labelledby="terminals-list-title"
  >
    <div class="secondary-pane__header">
      <span id="terminals-list-title" class="secondary-pane__title">
        {t('terminals.title', 'Terminals')}
      </span>
      <Button
        variant="primary"
        disabled={serverUnavailable}
        onClick={openStartDialog}
      >
        <svg viewBox="0 0 14 14" width="11" height="11" aria-hidden="true">
          <path d="M7 1v12M1 7h12" />
        </svg>
        {t('common.add', 'Add')}
      </Button>
    </div>

    <div class="secondary-pane__scroll secondary-list terminals-view__list">
      {#if viewState.loading && viewState.terminals.length === 0}
        <Banner variant="neutral">
          {t('terminals.loading', 'Loading terminal sessions…')}
        </Banner>
      {:else if viewState.listError && !serverUnavailable}
        <Banner variant="error">
          <span
            >{t(
              'terminals.listError',
              'Terminal sessions could not be loaded.',
            )}</span
          >
          <Button
            variant="secondary"
            onClick={() => controller.loadTerminals()}
          >
            {t('common.retry', 'Retry')}
          </Button>
        </Banner>
      {:else if viewState.terminals.length === 0}
        <EmptyState
          title={t('terminals.emptyTitle', 'No terminal sessions')}
          description={t(
            'terminals.emptyDescription',
            'Open a manual terminal here, or monitor Terminal Sessions started by an agent.',
          )}
        />
      {:else}
        {#each viewState.terminals as item (item.terminal_id)}
          <button
            type="button"
            class="secondary-list__item terminals-view__list-item"
            class:active={item.terminal_id === viewState.selectedTerminalId}
            aria-current={item.terminal_id === viewState.selectedTerminalId
              ? 'true'
              : undefined}
            onclick={() => controller.selectTerminal(item.terminal_id)}
          >
            <span class="terminals-view__item-topline">
              <span
                class="terminals-view__item-title"
                use:tooltip={terminalTitle(item)}>{terminalTitle(item)}</span
              >
              <span
                class={`terminals-view__state-dot terminals-view__state-dot--${item.state}`}
                role="img"
                aria-label={stateLabel(item.state)}
                use:tooltip={stateLabel(item.state)}
              ></span>
            </span>
            <span class="terminals-view__target">{terminalTarget(item)}</span>
            <span class="terminals-view__item-meta">
              <span use:tooltip={terminalSession(item)}
                >{shortSession(item)}</span
              >
              <span>{startedAt(item)}</span>
            </span>
          </button>
        {/each}
      {/if}
    </div>
  </aside>

  <div class="terminals-view__detail">
    <header class="terminals-view__header view-header">
      <div class="view-header__intro">
        <p class="view-header__eyebrow">
          {t('terminals.eyebrow', 'Live PTY / ConPTY')}
        </p>
        <h2 id="terminals-title" class="view-header__title">
          {t('terminals.title', 'Terminals')}
        </h2>
        <p class="view-header__subtitle">
          {t(
            'terminals.subtitle',
            'Open and use your own terminals, or watch and control the same interactive terminal an agent is using.',
          )}
        </p>
      </div>
    </header>

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
        aria-label={t('terminals.canvasLabel', 'Terminal canvas')}
        style="grid-template-columns: repeat({layout.columns}, minmax(0, 1fr)); grid-template-rows: repeat({layout.rows}, minmax(0, 1fr));"
      >
        {#each viewState.terminals as item, itemIndex (item.terminal_id)}
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
          <div
            class="terminals-view__tile"
            class:terminals-view__tile--hidden={maximizedTerminalId &&
              !isMaximized}
            class:terminals-view__tile--maximized={isMaximized}
            class:terminals-view__tile--focused={isFocused &&
              !maximizedTerminalId}
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
                    loading={closingTerminalId === item.terminal_id}
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
        title={t('terminals.detailEmptyTitle', 'Open a terminal')}
        description={t(
          'terminals.detailEmptyDescription',
          'Start the local default shell or choose a command such as codex. Agent terminals will appear here too.',
        )}
      >
        {#snippet actions()}
          <Button
            variant="primary"
            disabled={serverUnavailable}
            onClick={openStartDialog}
          >
            {t('terminals.new', 'New terminal')}
          </Button>
        {/snippet}
      </EmptyState>
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

<style>
  .terminals-view {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    background: var(--bg);
  }

  .terminals-view__list-pane {
    gap: 0;
  }

  .terminals-view__list-item {
    display: flex;
    width: 100%;
    flex-direction: column;
    gap: 6px;
    text-align: left;
  }

  .terminals-view__item-topline,
  .terminals-view__item-meta {
    display: flex;
    align-items: center;
  }

  .terminals-view__item-topline,
  .terminals-view__item-meta {
    width: 100%;
    justify-content: space-between;
    gap: 8px;
  }

  .terminals-view__item-title,
  .terminals-view__target,
  .terminals-view__item-meta {
    overflow: hidden;
    font-family: var(--font-mono);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .terminals-view__item-title {
    color: var(--text-hi);
    font-size: var(--fs-mono-body);
    font-weight: 500;
  }

  .terminals-view__target {
    width: 100%;
    color: var(--text-med);
    font-size: var(--fs-mono-sm);
  }

  .terminals-view__item-meta {
    color: var(--text-lo);
    font-size: var(--fs-mono-xs);
  }

  .terminals-view__state-dot {
    width: 7px;
    height: 7px;
    flex: 0 0 auto;
    border-radius: 50%;
    background: var(--text-lo);
  }

  .terminals-view__state-dot--working,
  .terminals-view__state-dot--ready {
    background: var(--green);
  }

  .terminals-view__state-dot--starting {
    background: var(--amber);
  }

  .terminals-view__state-dot--error {
    background: var(--red);
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

  .terminals-view__header {
    padding: 0 0 14px;
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
    opacity: 1 !important;
    pointer-events: auto !important;
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
      flex-direction: column;
    }

    .terminals-view__list-pane {
      max-height: 220px;
      flex: 0 0 auto;
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
