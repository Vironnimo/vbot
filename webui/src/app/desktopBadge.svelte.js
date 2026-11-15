// The Desktop's taskbar badge (capability `taskbarBadge`): the app-level owner
// that tells the Desktop how many results are unread. While the user looks at
// the page (visible and focused) that is what the Main menu counts; otherwise
// the displayed Session's result counts too, because nobody is reading it.
import { setDesktopTaskbarBadge } from '$lib/desktopBridge.js';

const pageAttended = () =>
  document.visibilityState === 'visible' && document.hasFocus();

/**
 * Create the badge owner while a component or effect root initializes.
 * `context.supported` tells whether the Desktop shows a badge;
 * `context.attendedCount` and `context.unattendedCount` are the unread
 * results in each case.
 */
export function createDesktopBadge(context) {
  let attended = $state(pageAttended());
  // The latest count still to send; counts that change while one is under way
  // collapse into it, so the Desktop ends at the last one.
  let wanted = null;
  let sent = null;
  let sending = false;

  async function flush() {
    if (sending) return;
    sending = true;
    try {
      while (wanted !== null && wanted !== sent) {
        const count = wanted;
        try {
          await setDesktopTaskbarBadge(count);
          sent = count;
        } catch {
          // The badge is a hint: the next change sends the count again.
          wanted = null;
        }
      }
    } finally {
      sending = false;
    }
  }

  $effect(() => {
    if (!context.supported) return;
    const count = attended ? context.attendedCount : context.unattendedCount;
    wanted = Number.isSafeInteger(count) && count > 0 ? count : 0;
    void flush();
  });

  $effect(() => {
    const update = () => {
      attended = pageAttended();
    };
    document.addEventListener('visibilitychange', update);
    window.addEventListener('focus', update);
    window.addEventListener('blur', update);
    return () => {
      document.removeEventListener('visibilitychange', update);
      window.removeEventListener('focus', update);
      window.removeEventListener('blur', update);
      // The page stops counting (it lost its access to the server).
      if (sent) void setDesktopTaskbarBadge(0).catch(() => {});
    };
  });
}
