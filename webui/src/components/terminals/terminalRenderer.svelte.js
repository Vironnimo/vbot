import { SvelteMap } from 'svelte/reactivity';
import { t } from '$lib/i18n.js';
import {
  TERMINAL_MAX_COLUMNS,
  TERMINAL_MAX_ROWS,
  TERMINAL_STREAM_ERROR,
  clampTerminalGrid,
  terminalIsFinished,
} from '$lib/terminalsView.js';

// Internal xterm lifecycle: buffers, renderer loading, geometry, focus and cleanup.
export function createTerminalRenderer({
  viewState,
  findTerminal,
  getController,
  isMounted,
  isUnavailable,
  getMaximizedTerminalId,
}) {
  let pendingFocusTerminalId = '';

  let scrolledBackByTerminal = $state({});

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
      !isMounted() ||
      !tileHosts.has(terminalId) ||
      tileRegistry.has(terminalId) ||
      rendererPromises.has(terminalId)
    ) {
      return;
    }
    const loading = initializeTerminal(terminalId)
      .catch(() => {
        if (isMounted()) {
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
    for (const disposable of tile.protocolDisposables) {
      disposable.dispose();
    }
    tile.xterm?.dispose();
  }

  // WebGL is intentionally not used: its renderer sizes the canvas backing
  // store from the browser's device-pixel box, which sits one pixel off the
  // cell grid at fractional desktop scale (Windows 125% / 150%) and paints
  // regular white row gaps that no post-hoc detection could fully rule out.
  // The DOM renderer is deterministic and fast enough for a handful of tiles.

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
    if (!isMounted() || !host) {
      return;
    }
    const interactive =
      !terminalIsFinished(findTerminal(terminalId)) && !isUnavailable();
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
    // The server answers terminal queries from its canonical screen. Viewer
    // replies would duplicate them and be mistaken for operator keystrokes.
    const protocolDisposables = [
      { final: 'c' },
      { prefix: '>', final: 'c' },
      { final: 'n' },
      { prefix: '?', final: 'n' },
      { intermediates: '$', final: 'p' },
      { prefix: '?', intermediates: '$', final: 'p' },
    ].map((id) => xtermInstance.parser.registerCsiHandler(id, () => true));
    protocolDisposables.push(
      xtermInstance.parser.registerCsiHandler({ final: 't' }, (params) =>
        [14, 16, 18, 20, 21].includes(params[0]),
      ),
      xtermInstance.parser.registerDcsHandler(
        { intermediates: '$', final: 'q' },
        () => true,
      ),
      ...[4, 10, 11, 12].map((id) =>
        xtermInstance.parser.registerOscHandler(id, (data) =>
          data.split(';').includes('?'),
        ),
      ),
    );
    const inputDisposable = xtermInstance.onData((data) => {
      // Focus reports describe this viewer, not an input to the shared TUI.
      if (data === '\u001b[I' || data === '\u001b[O') {
        return;
      }
      if (!terminalIsFinished(findTerminal(terminalId))) {
        getController().queueInput(data, { terminalId });
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
      protocolDisposables,
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
  // chance to settle and lets the getController()'s stability pass see the
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
      getController().resize(
        fitted.columns,
        fitted.rows,
        terminalId,
        getMaximizedTerminalId() === terminalId,
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

  // Diagnostics: a tile whose settled grid differs from the server's
  // authoritative dimensions renders TUI content at the wrong cell count —
  // stretched or wrapped borders. While a resize correction is still inside
  // the pipeline (stability pass, debounce, in-flight request) the mismatch
  // is expected and stays quiet; a lit hint means the pipeline closed
  // without reconciling the two sizes.
  function gridMismatchHint(terminalId) {
    const fitted = fittedGrids[terminalId];
    const item = findTerminal(terminalId);
    if (!fitted || !item || terminalIsFinished(item) || isUnavailable()) {
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

  function onSnapshot(terminalId, ansi, snapshotTerminal) {
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
  }

  function onOutput(terminalId, data) {
    const tile = tileRegistry.get(terminalId);
    if (tile) {
      tile.xterm.write(data);
    } else {
      const queued = pendingOutputs.get(terminalId) ?? [];
      queued.push(data);
      pendingOutputs.set(terminalId, queued.slice(-256));
    }
  }

  function onClear(terminalId) {
    pendingSnapshots.delete(terminalId);
    pendingOutputs.delete(terminalId);
    scrolledBackByTerminal[terminalId] = false;
    tileRegistry.get(terminalId)?.xterm?.reset();
  }

  function onTranscript(terminalId, text) {
    const tile = tileRegistry.get(terminalId);
    tile?.xterm.paste(text);
    if (viewState.selectedTerminalId === terminalId) tile?.xterm.focus();
  }

  function fitAll() {
    for (const id of tileRegistry.keys()) scheduleFit(id);
  }
  function updateInteractivity(items) {
    for (const item of items) {
      const tile = tileRegistry.get(item.terminal_id);
      if (!tile?.xterm) continue;
      const interactive = !terminalIsFinished(item) && !isUnavailable();
      tile.xterm.options.disableStdin = !interactive;
      tile.xterm.options.cursorBlink = interactive;
    }
  }
  function focus(terminalId) {
    const tile = tileRegistry.get(terminalId);
    if (tile?.xterm) tile.xterm.focus();
    else pendingFocusTerminalId = terminalId;
  }
  function destroy() {
    for (const id of [...tileRegistry.keys()]) disposeTile(id);
  }
  return {
    mountTile,
    scrollToLatest,
    gridMismatchHint,
    onSnapshot,
    onOutput,
    onClear,
    onTranscript,
    fitAll,
    updateInteractivity,
    focus,
    destroy,
    hasRenderer: (id) => tileRegistry.has(id),
    scrolledBack: (id) => scrolledBackByTerminal[id],
  };
}
