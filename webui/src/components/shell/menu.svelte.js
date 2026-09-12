import { SvelteSet, SvelteURL } from 'svelte/reactivity';
import { t } from '$lib/i18n.js';
import {
  getDesktopClipboardText,
  openDesktopExternalUrl,
  setDesktopClipboardText,
} from '$lib/desktopBridge.js';

export function createDesktopContextMenu(context) {
  const CONTEXT_MENU_VIEWPORT_MARGIN = 8;

  const TEXT_INPUT_TYPES = new SvelteSet([
    'email',
    'password',
    'search',
    'tel',
    'text',
    'url',
  ]);

  let contextMenuElement = $state(null);

  let contextMenu = $state(null);

  const composedPath = (event) =>
    typeof event.composedPath === 'function'
      ? event.composedPath()
      : [event.target];

  const linkFromPath = (path) =>
    path.find((node) => node instanceof HTMLAnchorElement) ?? null;

  const editableFromPath = (path) => {
    for (const node of path) {
      if (node instanceof HTMLTextAreaElement) {
        return {
          element: node,
          writable: !node.disabled && !node.readOnly,
          kind: 'control',
        };
      }
      if (node instanceof HTMLInputElement && TEXT_INPUT_TYPES.has(node.type)) {
        return {
          element: node,
          writable: !node.disabled && !node.readOnly,
          kind: 'control',
          sensitive: node.type === 'password',
        };
      }
      if (node instanceof HTMLElement && node.isContentEditable) {
        return { element: node, writable: true, kind: 'contenteditable' };
      }
    }
    return null;
  };

  const safeExternalUrl = (anchor) => {
    if (!anchor) return '';
    try {
      const url = new SvelteURL(anchor.href, window.location.href);
      return ['http:', 'https:'].includes(url.protocol) && url.hostname
        ? url.href
        : '';
    } catch {
      return '';
    }
  };

  const selectionForEditable = (editable) => {
    if (!editable) return null;
    if (editable.kind === 'control') {
      const start = editable.element.selectionStart ?? 0;
      const end = editable.element.selectionEnd ?? start;
      return {
        kind: editable.kind,
        element: editable.element,
        start,
        end,
        text: editable.sensitive
          ? ''
          : editable.element.value.slice(start, end),
      };
    }

    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) {
      return {
        kind: editable.kind,
        element: editable.element,
        range: null,
        text: '',
      };
    }
    const range = selection.getRangeAt(0);
    if (!editable.element.contains(range.commonAncestorContainer)) {
      return {
        kind: editable.kind,
        element: editable.element,
        range: null,
        text: '',
      };
    }
    return {
      kind: editable.kind,
      element: editable.element,
      range: range.cloneRange(),
      text: selection.toString(),
    };
  };

  const selectionAtTarget = (target) => {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return null;
    }
    const range = selection.getRangeAt(0);
    try {
      if (!(target instanceof Node) || !range.intersectsNode(target)) {
        return null;
      }
    } catch {
      return null;
    }
    const text = selection.toString();
    return text ? { text, range: range.cloneRange() } : null;
  };

  const contextMenuPosition = (event) => {
    if (event.clientX || event.clientY) {
      return { x: event.clientX, y: event.clientY };
    }
    const rect = event.target?.getBoundingClientRect?.();
    return {
      x: rect?.left ?? CONTEXT_MENU_VIEWPORT_MARGIN,
      y: rect?.bottom ?? CONTEXT_MENU_VIEWPORT_MARGIN,
    };
  };

  const handleContextMenu = (event) => {
    if (!context.desktopContextMenuEnabled) return;

    const path = composedPath(event);
    const editable = editableFromPath(path);
    const editableSelection = selectionForEditable(editable);
    const selectedText = editable
      ? editableSelection
      : selectionAtTarget(event.target);
    const url = safeExternalUrl(linkFromPath(path));
    const actions = [];

    if (url) {
      actions.push(
        {
          id: 'copy-link',
          group: 'link',
          label: t('desktop.contextMenu.copyLinkAddress', 'Copy link address'),
        },
        {
          id: 'open-link',
          group: 'link',
          label: t('desktop.contextMenu.openInBrowser', 'Open in browser'),
        },
      );
    }
    if (editable) {
      if (selectedText?.text && editable.writable) {
        actions.push({
          id: 'cut',
          group: 'edit',
          label: t('desktop.contextMenu.cut', 'Cut'),
        });
      }
      if (selectedText?.text) {
        actions.push({
          id: 'copy',
          group: 'edit',
          label: t('common.copy', 'Copy'),
        });
      }
      if (editable.writable) {
        actions.push({
          id: 'paste',
          group: 'edit',
          label: t('desktop.contextMenu.paste', 'Paste'),
        });
      }
    } else if (selectedText?.text) {
      actions.push({
        id: 'copy',
        group: 'selection',
        label: t('common.copy', 'Copy'),
      });
    }

    if (actions.length === 0) return;

    event.preventDefault();
    const position = contextMenuPosition(event);
    contextMenu = {
      ...position,
      positioned: false,
      actions,
      editable,
      selection: selectedText,
      url,
      focusTarget:
        editable?.element ??
        (event.target instanceof HTMLElement ? event.target : null),
    };
  };

  const restoreContextFocus = (target) => {
    if (!(target instanceof HTMLElement) || !target.isConnected) return;
    queueMicrotask(() => target.focus({ preventScroll: true }));
  };

  const closeContextMenu = ({ restoreFocus = false } = {}) => {
    const focusTarget = contextMenu?.focusTarget;
    contextMenu = null;
    if (restoreFocus) restoreContextFocus(focusTarget);
  };

  const dispatchEditInput = (element, inputType, data = null) => {
    const event =
      typeof InputEvent === 'function'
        ? new InputEvent('input', { bubbles: true, inputType, data })
        : new Event('input', { bubbles: true });
    element.dispatchEvent(event);
  };

  const replaceEditableSelection = (selection, replacement, inputType) => {
    if (!selection) return;
    selection.element.focus({ preventScroll: true });
    if (selection.kind === 'control') {
      selection.element.setSelectionRange(selection.start, selection.end);
      selection.element.setRangeText(
        replacement,
        selection.start,
        selection.end,
        'end',
      );
      dispatchEditInput(selection.element, inputType, replacement || null);
      return;
    }
    if (!selection.range) return;
    const range = selection.range;
    range.deleteContents();
    if (replacement) {
      const textNode = document.createTextNode(replacement);
      range.insertNode(textNode);
      range.setStartAfter(textNode);
    }
    range.collapse(true);
    const browserSelection = window.getSelection();
    browserSelection?.removeAllRanges();
    browserSelection?.addRange(range);
    dispatchEditInput(selection.element, inputType, replacement || null);
  };

  const notifyContextMenuFailure = () => {
    context.onToast({
      title: t(
        'desktop.contextMenu.actionFailedTitle',
        'Desktop action failed',
      ),
      message: t(
        'desktop.contextMenu.actionFailedMessage',
        'The clipboard or default browser could not complete the action.',
      ),
      variant: 'warn',
    });
  };

  const handleContextMenuAction = async (actionId) => {
    const snapshot = contextMenu;
    if (!snapshot) return;
    contextMenu = null;
    try {
      if (actionId === 'copy-link') {
        await setDesktopClipboardText(snapshot.url);
      } else if (actionId === 'open-link') {
        await openDesktopExternalUrl(snapshot.url);
      } else if (actionId === 'copy') {
        await setDesktopClipboardText(snapshot.selection?.text ?? '');
      } else if (actionId === 'cut') {
        await setDesktopClipboardText(snapshot.selection?.text ?? '');
        replaceEditableSelection(snapshot.selection, '', 'deleteByCut');
      } else if (actionId === 'paste') {
        const clipboardText = await getDesktopClipboardText();
        replaceEditableSelection(
          snapshot.selection,
          clipboardText,
          'insertFromPaste',
        );
      }
    } catch {
      notifyContextMenuFailure();
    } finally {
      restoreContextFocus(snapshot.focusTarget);
    }
  };

  const handleContextMenuKeydown = (event) => {
    const items = Array.from(
      contextMenuElement?.querySelectorAll('[role="menuitem"]') ?? [],
    );
    const currentIndex = items.indexOf(document.activeElement);
    let nextIndex = null;
    if (event.key === 'ArrowDown') {
      nextIndex = (currentIndex + 1) % items.length;
    } else if (event.key === 'ArrowUp') {
      nextIndex = (currentIndex - 1 + items.length) % items.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = items.length - 1;
    } else if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeContextMenu({ restoreFocus: true });
      return;
    }
    if (nextIndex === null || items.length === 0) return;
    event.preventDefault();
    items[nextIndex].focus({ preventScroll: true });
  };

  const handleWindowPointerDown = (event) => {
    if (contextMenu && !contextMenuElement?.contains(event.target)) {
      closeContextMenu();
    }
  };

  const handleWindowKeydown = (event) => {
    if (contextMenu && event.key === 'Escape') {
      closeContextMenu({ restoreFocus: true });
    }
  };
  $effect(() => {
    if (!contextMenu || contextMenu.positioned || !contextMenuElement) {
      return undefined;
    }
    const menuSnapshot = contextMenu;
    const frame = requestAnimationFrame(() => {
      if (contextMenu !== menuSnapshot || !contextMenuElement) return;
      const bounds = contextMenuElement.getBoundingClientRect();
      const maximumX = Math.max(
        CONTEXT_MENU_VIEWPORT_MARGIN,
        window.innerWidth - bounds.width - CONTEXT_MENU_VIEWPORT_MARGIN,
      );
      const maximumY = Math.max(
        CONTEXT_MENU_VIEWPORT_MARGIN,
        window.innerHeight - bounds.height - CONTEXT_MENU_VIEWPORT_MARGIN,
      );
      contextMenu = {
        ...contextMenu,
        x: Math.min(
          Math.max(contextMenu.x, CONTEXT_MENU_VIEWPORT_MARGIN),
          maximumX,
        ),
        y: Math.min(
          Math.max(contextMenu.y, CONTEXT_MENU_VIEWPORT_MARGIN),
          maximumY,
        ),
        positioned: true,
      };
      contextMenuElement
        .querySelector('[role="menuitem"]')
        ?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  });

  return {
    get contextMenuElement() {
      return contextMenuElement;
    },
    set contextMenuElement(value) {
      contextMenuElement = value;
    },
    get contextMenu() {
      return contextMenu;
    },
    set contextMenu(value) {
      contextMenu = value;
    },
    get handleContextMenu() {
      return handleContextMenu;
    },
    get closeContextMenu() {
      return closeContextMenu;
    },
    get handleContextMenuAction() {
      return handleContextMenuAction;
    },
    get handleContextMenuKeydown() {
      return handleContextMenuKeydown;
    },
    get handleWindowPointerDown() {
      return handleWindowPointerDown;
    },
    get handleWindowKeydown() {
      return handleWindowKeydown;
    },
  };
}
