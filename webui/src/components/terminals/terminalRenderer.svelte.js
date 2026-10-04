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

  const TERMINAL_BASE_FONT_SIZE = 12;

  const TERMINAL_MIN_FONT_SIZE = 4;

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
              error: t('terminals.rendererError'),
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
    tile.host.removeEventListener('pointerdown', tile.claimOnPointer, true);
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
      fontFamily: cssToken('--font-mono', 'Geist Mono, monospace'),
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
        claim(terminalId);
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
      resizeObserverInstance = new ResizeObserver(() => {
        // Only a real change of this tile's box is a layout change here;
        // adopting another viewer's grid never resizes the host.
        const tile = tileRegistry.get(terminalId);
        if (
          tile &&
          (tile.hostWidth !== host.clientWidth ||
            tile.hostHeight !== host.clientHeight)
        ) {
          tile.hostWidth = host.clientWidth;
          tile.hostHeight = host.clientHeight;
          claim(terminalId, { refit: true });
        }
      });
      resizeObserverInstance.observe(host);
    }
    const claimOnPointer = () => claim(terminalId);
    host.addEventListener('pointerdown', claimOnPointer, true);
    tileRegistry.set(terminalId, {
      xterm: xtermInstance,
      fitAddon: fitAddonInstance,
      host,
      resizeObserver: resizeObserverInstance,
      claimOnPointer,
      inputDisposable,
      scrollDisposable,
      protocolDisposables,
      // A claimed tile sizes the shared PTY to its own box; an unclaimed one
      // mirrors the grid another viewer set. Opening a tile claims it.
      claimed: true,
      fittedGrid: null,
      hostWidth: host.clientWidth,
      hostHeight: host.clientHeight,
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
    scrolledBackByTerminal[terminalId] = false;
    try {
      // The in-band reset (RIS) takes effect after output that is still
      // queued in xterm, so no older bytes land on top of the snapshot.
      tile.xterm.write(`\u001bc${ansi}`, () => {
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

  // Interaction with a tile (typing, pointer, a change of its box) makes it
  // the viewer the shared PTY follows; other viewers mirror its grid.
  function claim(terminalId, { refit = false } = {}) {
    const tile = tileRegistry.get(terminalId);
    if (!tile || (tile.claimed && !refit)) {
      return;
    }
    tile.claimed = true;
    scheduleFit(terminalId);
  }

  function fitTerminal(terminalId) {
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
      // A hidden tile keeps the PTY at its current size.
      return;
    }
    try {
      if (tile.claimed) {
        fitToTile(terminalId, tile);
      } else {
        adoptServerGrid(terminalId, tile);
      }
    } catch {
      // The host may be between layout states while the view is mounting.
    }
  }

  function fitToTile(terminalId, tile) {
    if (tile.xterm.options.fontSize !== TERMINAL_BASE_FONT_SIZE) {
      tile.xterm.options.fontSize = TERMINAL_BASE_FONT_SIZE;
    }
    tile.fitAddon.fit();
    // Only a tile too large for the PTY limits grows the font. Font metrics
    // are not perfectly proportional, so this may need more than one pass.
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
    // Clamp into the server's accepted bounds so a tiny host produces a
    // legal minimum grid instead of a rejected resize.
    const fitted = clampTerminalGrid(tile.xterm.cols, tile.xterm.rows);
    if (fitted.columns !== tile.xterm.cols || fitted.rows !== tile.xterm.rows) {
      tile.xterm.resize(fitted.columns, fitted.rows);
    }
    tile.fittedGrid = fitted;
    getController().resize(
      fitted.columns,
      fitted.rows,
      terminalId,
      getMaximizedTerminalId() === terminalId,
    );
  }

  // Output is laid out for the PTY grid, so a viewer that does not own the
  // size renders exactly that grid rather than its own fit. A grid larger
  // than the tile shrinks the font instead of being clipped.
  function adoptServerGrid(terminalId, tile) {
    const item = findTerminal(terminalId);
    if (!Number.isInteger(item?.columns) || !Number.isInteger(item?.rows)) {
      return;
    }
    let fontSize = TERMINAL_BASE_FONT_SIZE;
    for (let attempts = 0; attempts < 5; attempts += 1) {
      if (tile.xterm.options.fontSize !== fontSize) {
        tile.xterm.options.fontSize = fontSize;
      }
      const room = tile.fitAddon.proposeDimensions?.();
      if (
        !room ||
        (room.cols >= item.columns && room.rows >= item.rows) ||
        fontSize <= TERMINAL_MIN_FONT_SIZE
      ) {
        break;
      }
      // Font metrics are not perfectly proportional; step down at least
      // half a point per pass.
      const scale = Math.min(room.cols / item.columns, room.rows / item.rows);
      fontSize = Math.max(
        TERMINAL_MIN_FONT_SIZE,
        Math.min(fontSize - 0.5, Math.floor(fontSize * scale * 2) / 2),
      );
    }
    if (tile.xterm.cols !== item.columns || tile.xterm.rows !== item.rows) {
      tile.xterm.resize(item.columns, item.rows);
    }
  }

  // The PTY grid changed. Unless this viewer's own resize explains it, a
  // different grid means another viewer or the Agent took over the size.
  function onGeometry(terminalId, terminal) {
    const tile = tileRegistry.get(terminalId);
    if (
      !tile?.xterm ||
      !Number.isInteger(terminal?.columns) ||
      !Number.isInteger(terminal?.rows) ||
      (tile.xterm.cols === terminal.columns &&
        tile.xterm.rows === terminal.rows) ||
      getController().resizeSettling(terminalId)
    ) {
      return;
    }
    tile.claimed =
      tile.fittedGrid?.columns === terminal.columns &&
      tile.fittedGrid.rows === terminal.rows;
    scheduleFit(terminalId);
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
      foreground: cssToken('--text-hi', '#F1EDE6'),
      cursor: cssToken('--accent', '#E8870A'),
      cursorAccent: cssToken('--terminal-surface', '#0E0D0B'),
      selectionBackground: cssToken('--border-2', '#423C35'),
      black: cssToken('--terminal-surface', '#0E0D0B'),
      red: cssToken('--red', '#F28B82'),
      green: cssToken('--green', '#5FCF8F'),
      yellow: cssToken('--amber', '#E5B53E'),
      blue: cssToken('--blue', '#74ADF2'),
      magenta: '#D8A4E2',
      cyan: '#67D4C1',
      white: cssToken('--text-hi', '#F1EDE6'),
      brightBlack: cssToken('--text-lo', '#9D9387'),
      brightRed: '#FFA0A0',
      brightGreen: '#7AE7A4',
      brightYellow: '#FBC45B',
      brightBlue: '#8EC0FF',
      brightMagenta: '#E7BDEE',
      brightCyan: '#8DE1D2',
      brightWhite: '#FFF9F0',
    };
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
    onSnapshot,
    onOutput,
    onGeometry,
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
