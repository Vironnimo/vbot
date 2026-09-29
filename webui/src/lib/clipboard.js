// Plain-text clipboard writes for WebUI copy actions. Inside the Desktop app
// the text goes through its native clipboard, because the selected server can
// be remote plain HTTP, where the browser Clipboard API is unavailable; other
// accessors use the browser Clipboard API. Callers report success or failure.
import { isDesktopAccessor, setDesktopClipboardText } from './desktopBridge.js';

/** Replace the clipboard content with `text`; rejects when it cannot. */
export async function writeClipboardText(text) {
  if (isDesktopAccessor()) await setDesktopClipboardText(text);
  else await navigator.clipboard.writeText(text);
}
