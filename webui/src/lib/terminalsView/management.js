import {
  visibleTerminals,
  reconcileTerminalLaunchHistory,
  mergeTerminalSummary,
  reconcileGroup,
  reconcileSingleGroup,
  errorMessage,
} from './state.js';

export function createTerminalManagement({
  state,
  api,
  isDestroyed,
  isUnavailable,
  loadTerminals,
  reconcileStreams,
}) {
  async function createGroup(name) {
    if (state.groupActionPending || !name?.trim() || isUnavailable()) {
      return null;
    }
    state.groupActionPending = true;
    state.actionError = '';
    try {
      const result = await api.createTerminalGroup(name.trim());
      const group = reconcileSingleGroup(state, result?.group);
      if (group) {
        state.selectedGroupId = group.group_id;
      }
      return group;
    } catch (error) {
      state.actionError = errorMessage(error);
      return null;
    } finally {
      state.groupActionPending = false;
    }
  }

  async function renameGroup(groupId, name) {
    if (
      state.groupActionPending ||
      !groupId ||
      !name?.trim() ||
      isUnavailable()
    ) {
      return false;
    }
    state.groupActionPending = true;
    state.actionError = '';
    try {
      const group = await api.renameTerminalGroup(groupId, name.trim());
      reconcileGroup(state, group?.group);
      return true;
    } catch (error) {
      state.actionError = errorMessage(error);
      return false;
    } finally {
      state.groupActionPending = false;
    }
  }

  async function deleteGroup(groupId) {
    if (state.groupActionPending || !groupId || isUnavailable()) {
      return null;
    }
    state.groupActionPending = true;
    state.actionError = '';
    try {
      const result = await api.deleteTerminalGroup(groupId);
      await loadTerminals({ silent: true });
      return (
        result ?? {
          group_id: groupId,
          terminals_killed: 0,
        }
      );
    } catch (error) {
      state.actionError = errorMessage(error);
      return null;
    } finally {
      state.groupActionPending = false;
    }
  }

  function reorderGroup(groupId, order) {
    if (!groupId || !Array.isArray(order) || isUnavailable()) {
      return;
    }
    const current = visibleTerminals(state);
    const members = new Set(current.map((terminal) => terminal.terminal_id));
    const ordered = order.filter((terminalId) => members.has(terminalId));
    for (const terminal of current) {
      if (!ordered.includes(terminal.terminal_id)) {
        ordered.push(terminal.terminal_id);
      }
    }
    // Optimistic local order: rewrite the terminal list in the new order so
    // the canvas reflows immediately; the server order follows.
    state.terminals = state.terminals
      .filter((terminal) => terminal.group_id !== groupId)
      .concat(
        ordered.map((terminalId) =>
          state.terminals.find(
            (terminal) => terminal.terminal_id === terminalId,
          ),
        ),
      );
    void api.setTerminalGroupOrder(groupId, ordered).catch(() => {
      if (!isDestroyed()) {
        state.actionError = errorMessage(
          new Error(
            'The terminal order could not be saved on the server. Reload the list to restore it.',
          ),
        );
      }
    });
  }

  async function startManualTerminal(params = {}, { groupId = null } = {}) {
    if (state.startingTerminal || isUnavailable()) {
      return null;
    }
    state.startingTerminal = true;
    state.startError = '';
    try {
      const request = { ...params };
      if (groupId) {
        request.group_id = groupId;
      }
      const result = await api.startTerminal(request);
      if (isDestroyed()) {
        return null;
      }
      const terminal = mergeTerminalSummary(state, result?.terminal);
      if (!terminal) {
        throw new Error('The server returned an invalid terminal.');
      }
      reconcileTerminalLaunchHistory(state, result);
      state.selectedGroupId =
        terminal.group_id || groupId || state.selectedGroupId;
      // The append tile marks the next position, including after a reload.
      // Save through the existing group-order contract instead of relying on
      // the catalog's default newest-first ordering.
      reorderGroup(state.selectedGroupId, [
        ...visibleTerminals(state)
          .filter((item) => item.terminal_id !== terminal.terminal_id)
          .map((item) => item.terminal_id),
        terminal.terminal_id,
      ]);
      state.selectedTerminalId = terminal.terminal_id;
      reconcileStreams();
      return terminal;
    } catch (error) {
      if (!isDestroyed()) {
        state.startError = errorMessage(error);
      }
      return null;
    } finally {
      state.startingTerminal = false;
    }
  }

  return {
    createGroup,
    renameGroup,
    deleteGroup,
    reorderGroup,
    startManualTerminal,
  };
}
