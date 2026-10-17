export const TERMINAL_STREAM_IDLE = 'idle';

export const TERMINAL_STREAM_CONNECTING = 'connecting';

export const TERMINAL_STREAM_CONNECTED = 'connected';

export const TERMINAL_STREAM_RECONNECTING = 'reconnecting';

export const TERMINAL_STREAM_ERROR = 'error';

export const TERMINAL_STREAM_SNAPSHOT = 'snapshot';

const TERMINAL_STATES_FINISHED = new Set(['exited', 'error']);

// The server validates Terminal dimensions against these exact bounds and
// rejects anything outside them. The viewer clamps every fitted grid into
// this window, so a small tile produces a legal minimum grid instead of a
// rejected request that would leave the tile rendering at a size the PTY
// never confirmed.
const TERMINAL_MIN_COLUMNS = 40;

export const TERMINAL_MAX_COLUMNS = 240;

const TERMINAL_MIN_ROWS = 10;

export const TERMINAL_MAX_ROWS = 80;

export function clampTerminalGrid(columns, rows) {
  return {
    columns: Math.min(
      Math.max(Math.floor(columns), TERMINAL_MIN_COLUMNS),
      TERMINAL_MAX_COLUMNS,
    ),
    rows: Math.min(
      Math.max(Math.floor(rows), TERMINAL_MIN_ROWS),
      TERMINAL_MAX_ROWS,
    ),
  };
}

/**
 * Fixed canvas grid layout for a terminal count.
 *
 * @param {number} count
 * @returns {{ rows: number, columns: number, spans: Array<{ row: number, column: number, rowSpan: number, columnSpan: number }> }}
 */
export function layoutForCount(count) {
  const total = Number.isInteger(count) && count > 0 ? count : 0;
  const columns =
    total === 0 ? 0 : total <= 2 ? total : total <= 4 ? 2 : total <= 6 ? 3 : 4;
  const rows = total === 0 ? 0 : Math.ceil(total / columns);
  const spans = [];
  for (let index = 0; index < total; index += 1) {
    let row;
    let column;
    if (total === 3) {
      row = index === 0 ? 0 : 1;
      column = index === 0 ? 0 : index - 1;
    } else {
      row = Math.floor(index / columns);
      column = index % columns;
    }
    const span = {
      row,
      column,
      rowSpan: 1,
      columnSpan: 1,
    };
    if (total === 3 && index === 0) {
      span.columnSpan = 2;
    }
    spans.push(span);
  }
  return { rows, columns, spans };
}

export function createTerminalsViewState() {
  return {
    terminals: [],
    groups: [],
    launchHistory: [],
    selectedGroupId: '',
    selectedTerminalId: '',
    loading: false,
    listError: '',
    actionError: '',
    streams: {},
    closing: '',
    groupActionPending: false,
    startingTerminal: false,
    startError: '',
    speechTerminalId: '',
    speechState: 'idle',
    // Finished terminals that stay on the canvas: they ended while their
    // view was shown, so their last screen does not vanish under the user.
    lingeringIds: [],
    // A finished terminal opened from the history, shown alone.
    historyTerminalId: '',
  };
}

/**
 * The terminals on the canvas: the running ones of the shown view and those
 * that finished while it was shown, or the one opened from the history.
 */
export function visibleTerminals(state) {
  if (state.historyTerminalId) {
    return state.terminals.filter(
      (terminal) => terminal.terminal_id === state.historyTerminalId,
    );
  }
  if (!state.selectedGroupId) {
    return [];
  }
  return state.terminals.filter(
    (terminal) =>
      terminal?.group_id === state.selectedGroupId &&
      (!terminalIsFinished(terminal) ||
        state.lingeringIds.includes(terminal.terminal_id)),
  );
}

/**
 * The views on the bar: the user's groups, every view with a running
 * terminal, and the shown one.
 */
export function shownViews(state) {
  return state.groups.filter(
    (group) =>
      group.kind === 'user' ||
      Number(group.live_count) > 0 ||
      group.group_id === state.selectedGroupId,
  );
}

/** Finished terminals of every view, the most recently finished first. */
export function finishedTerminals(state) {
  return state.terminals
    .filter((terminal) => terminalIsFinished(terminal))
    .sort((left, right) =>
      String(right.finished_at ?? '').localeCompare(
        String(left.finished_at ?? ''),
      ),
    );
}

/**
 * The view a new terminal started in *group* is pinned to: a group or a
 * conversation; the operator's own terminals need no pin.
 */
export function pinTargetOf(group) {
  return group?.kind === 'user' || group?.kind === 'session'
    ? group.group_id
    : null;
}

/** Whether a terminal can be moved into *group* by a drop or a menu. */
export function canMoveTo(terminal, group) {
  if (!terminal || !group || terminal.group_id === group.group_id) {
    return false;
  }
  return (
    group.group_id === terminal.home_group_id || pinTargetOf(group) !== null
  );
}

export function terminalIsFinished(terminal) {
  return TERMINAL_STATES_FINISHED.has(terminal?.state);
}

export function reconcileTerminalList(state, result) {
  const terminals = Array.isArray(result?.terminals) ? result.terminals : [];
  const shownLive = new Set(
    visibleTerminals(state)
      .filter((terminal) => !terminalIsFinished(terminal))
      .map((terminal) => terminal.terminal_id),
  );
  state.terminals = terminals.filter(
    (terminal) => terminal && typeof terminal.terminal_id === 'string',
  );
  keepLingering(state, shownLive);
  reconcileTerminalGroups(state, result);
  reconcileTerminalLaunchHistory(state, result);
  if (
    state.selectedGroupId &&
    !state.groups.some((group) => group.group_id === state.selectedGroupId)
  ) {
    state.selectedGroupId = defaultTerminalGroupId(state);
  }
  if (
    state.historyTerminalId &&
    !state.terminals.some(
      (terminal) => terminal.terminal_id === state.historyTerminalId,
    )
  ) {
    state.historyTerminalId = '';
  }
  if (
    state.selectedTerminalId &&
    state.terminals.some(
      (terminal) => terminal.terminal_id === state.selectedTerminalId,
    )
  ) {
    return state.selectedTerminalId;
  }
  const firstVisible = visibleTerminals(state)[0];
  state.selectedTerminalId = firstVisible?.terminal_id ?? '';
  return state.selectedTerminalId;
}

// A terminal shown running that is now finished stays on the canvas; one no
// longer listed leaves it.
function keepLingering(state, shownLive) {
  const listed = new Map(
    state.terminals.map((terminal) => [terminal.terminal_id, terminal]),
  );
  const lingering = state.lingeringIds.filter((terminalId) =>
    listed.has(terminalId),
  );
  for (const terminalId of shownLive) {
    const terminal = listed.get(terminalId);
    if (terminalIsFinished(terminal) && !lingering.includes(terminalId)) {
      lingering.push(terminalId);
    }
  }
  if (
    lingering.length !== state.lingeringIds.length ||
    lingering.some(
      (terminalId, index) => state.lingeringIds[index] !== terminalId,
    )
  ) {
    state.lingeringIds = lingering;
  }
}

function reconcileTerminalGroups(state, result) {
  if (!Array.isArray(result?.groups)) {
    return state.groups;
  }
  state.groups = result.groups.filter(
    (group) =>
      group &&
      typeof group.group_id === 'string' &&
      group.group_id &&
      (typeof group.name === 'string' || group.name === null),
  );
  if (!state.selectedGroupId) {
    state.selectedGroupId = defaultTerminalGroupId(state);
  }
  return state.groups;
}

// The view the Terminals view opens on: the first one with a running
// terminal, else the first group of the user, else the first view.
export function defaultTerminalGroupId(state) {
  const firstRunning =
    state.groups.find((group) => Number(group.live_count) > 0) ?? null;
  const firstUser = state.groups.find((group) => group.kind === 'user') ?? null;
  return (firstRunning ?? firstUser ?? state.groups[0])?.group_id ?? '';
}

export function reconcileTerminalLaunchHistory(state, result) {
  if (!Array.isArray(result?.launch_history)) {
    return state.launchHistory;
  }
  state.launchHistory = result.launch_history.filter(
    (entry) =>
      entry &&
      typeof entry.id === 'string' &&
      entry.id &&
      (entry.command === null || typeof entry.command === 'string') &&
      Array.isArray(entry.args) &&
      entry.args.every((argument) => typeof argument === 'string') &&
      (entry.workdir === null || typeof entry.workdir === 'string'),
  );
  return state.launchHistory;
}

export function mergeTerminalSummary(state, terminal) {
  if (!terminal || typeof terminal.terminal_id !== 'string') {
    return null;
  }
  const index = state.terminals.findIndex(
    (item) => item.terminal_id === terminal.terminal_id,
  );
  if (index < 0) {
    // The server places a new terminal last in its view.
    state.terminals = [...state.terminals, terminal];
    return terminal;
  }
  const shownLive = new Set(
    visibleTerminals(state)
      .filter((item) => !terminalIsFinished(item))
      .map((item) => item.terminal_id),
  );
  state.terminals[index] = { ...state.terminals[index], ...terminal };
  keepLingering(state, shownLive);
  return terminal;
}

export function reconcileGroup(state, group) {
  if (!group || typeof group.group_id !== 'string') {
    return null;
  }
  const index = state.groups.findIndex(
    (item) => item.group_id === group.group_id,
  );
  if (index < 0) {
    state.groups = [group, ...state.groups];
  } else {
    state.groups[index] = { ...state.groups[index], ...group };
  }
  return group;
}

export function reconcileSingleGroup(state, group) {
  const merged = reconcileGroup(state, group);
  if (merged) {
    reconcileTerminalList(state, {
      groups: state.groups,
      terminals: state.terminals,
    });
  }
  return merged;
}

export function errorMessage(error) {
  if (typeof error?.message === 'string' && error.message.trim()) {
    return error.message.trim();
  }
  if (typeof error === 'string' && error.trim()) {
    return error.trim();
  }
  return '';
}
