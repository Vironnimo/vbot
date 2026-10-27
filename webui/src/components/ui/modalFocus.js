// Shared lifetime for modal surfaces, including image previews. One stack
// owns keyboard focus so a lightbox opened over a form isolates that form too.
import { isImeComposing } from '$lib/keyboard.js';

const openModals = [];
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export const hasOpenModal = () => openModals.length > 0;

export function activateModalFocus({
  dialog,
  overlay = dialog,
  initialFocus = dialog,
  onClose,
  navigation,
}) {
  const previous = document.activeElement;
  const entry = {};
  const restored = [];
  openModals.push(entry);
  let branch = overlay;
  while (branch?.parentElement) {
    for (const sibling of branch.parentElement.children) {
      if (sibling === branch || sibling.inert) continue;
      sibling.inert = true;
      restored.push(sibling);
    }
    branch = branch.parentElement;
  }
  const releaseLayer = navigation?.registerLayer({ close: onClose });
  function handleKeydown(event) {
    if (openModals.at(-1) !== entry) return;
    if (isImeComposing(event)) {
      // Keep native IME cancellation, but do not let the same Escape close
      // a background layer through the app shell's window handler.
      if (event.key === 'Escape') event.stopPropagation();
      return;
    }

    if (event.key === 'Escape') {
      // A nested picker consumes Escape when dismissing its own popup.
      if (!event.defaultPrevented) onClose();
      return;
    }

    if (event.key !== 'Tab' || !dialog) {
      return;
    }

    const focusableElements = Array.from(
      dialog.querySelectorAll(FOCUSABLE_SELECTOR),
    );
    if (focusableElements.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }

    const firstElement = focusableElements[0];
    const lastElement = focusableElements.at(-1);
    const activeElement = document.activeElement;
    if (
      event.shiftKey &&
      (activeElement === dialog ||
        activeElement === firstElement ||
        !dialog.contains(activeElement))
    ) {
      event.preventDefault();
      lastElement.focus();
    } else if (
      !event.shiftKey &&
      (activeElement === dialog ||
        activeElement === lastElement ||
        !dialog.contains(activeElement))
    ) {
      event.preventDefault();
      firstElement.focus();
    }
  }

  document.addEventListener('keydown', handleKeydown);
  initialFocus?.focus();
  return () => {
    document.removeEventListener('keydown', handleKeydown);
    const index = openModals.indexOf(entry);
    if (index >= 0) openModals.splice(index, 1);
    releaseLayer?.();
    for (const element of restored) element.inert = false;
    if (previous?.isConnected && typeof previous.focus === 'function') {
      previous.focus();
    }
  };
}
