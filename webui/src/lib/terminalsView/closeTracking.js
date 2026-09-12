// A close removes the tile immediately while the server-side stop and
// catalog removal run in the background. The closing ids and failure
// listeners live at module scope so they survive a Terminals tab remount: a
// session closed before navigation must stay hidden from the remounted
// controller's list reloads until the background chain settles, and a
// failed stop must surface to whichever controller is mounted then.
export const closingIds = new Set();

export const closeFailureListeners = new Set();

export function notifyCloseFailed(terminalId, message) {
  for (const listener of closeFailureListeners) {
    listener(terminalId, message);
  }
}
