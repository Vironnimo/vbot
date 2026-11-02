// The server installation's update check (`application.update_status`) and
// whether this browser dismissed its notice. A dismissal names the version it
// dismissed, so the next newer version shows again. It lives in this browser's
// storage only; an unavailable storage shows the notice every time.

const DISMISSED_KEY = 'vbot.updateNoticeDismissed';

// The newer version the notice offers, or null.
export function availableUpdate(status) {
  const available = status?.available;
  return available && typeof available.version_id === 'string'
    ? available
    : null;
}

export function updateNoticeDismissed(versionId) {
  try {
    return localStorage.getItem(DISMISSED_KEY) === versionId;
  } catch {
    return false;
  }
}

export function dismissUpdateNotice(versionId) {
  try {
    localStorage.setItem(DISMISSED_KEY, versionId);
  } catch {
    // The notice simply shows again next time.
  }
}
