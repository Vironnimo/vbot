import { SvelteSet, SvelteURL } from 'svelte/reactivity';
import { t } from '$lib/i18n.js';
import {
  getDesktopClipboardText,
  openDesktopExternalUrl,
  setDesktopClipboardText,
} from '$lib/desktopBridge.js';
import { contextMenuAnchor } from '../ui/contextMenu.js';

// Desktop context menu: derives host-aware actions from a `contextmenu`
// event and runs them through the Desktop bridge. Rendering, positioning,
// keyboard navigation and dismissal belong to the shared
// `components/ui/ContextMenu.svelte`; `contextMenu` is its `menu` value.
export function createDesktopContextMenu(context) {
  const TEXT_INPUT_TYPES = new SvelteSet([
    'email',
    'password',
    'search',
    'tel',
    'text',
    'url',
  ]);

  let contextMenu = $state.raw(null);

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

  const handleContextMenu = (event) => {
    // A component that opened its own menu has already handled the event.
    if (!context.desktopContextMenuEnabled || event.defaultPrevented) return;

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
          label: t('desktop.contextMenu.copyLinkAddress'),
        },
        {
          id: 'open-link',
          group: 'link',
          label: t('desktop.contextMenu.openInBrowser'),
        },
      );
    }
    if (editable) {
      if (selectedText?.text && editable.writable) {
        actions.push({
          id: 'cut',
          group: 'edit',
          label: t('desktop.contextMenu.cut'),
        });
      }
      if (selectedText?.text) {
        actions.push({
          id: 'copy',
          group: 'edit',
          label: t('common.copy'),
        });
      }
      if (editable.writable) {
        actions.push({
          id: 'paste',
          group: 'edit',
          label: t('desktop.contextMenu.paste'),
        });
      }
    } else if (selectedText?.text) {
      actions.push({
        id: 'copy',
        group: 'selection',
        label: t('common.copy'),
      });
    }

    if (actions.length === 0) return;

    event.preventDefault();
    const snapshot = { selection: selectedText, url };
    const anchor = contextMenuAnchor(event);
    contextMenu = {
      x: anchor.x,
      y: anchor.y,
      returnFocus: editable?.element ?? anchor.returnFocus,
      label: t('desktop.contextMenu.label'),
      items: actions.map((action) => ({
        ...action,
        onSelect: () => runContextMenuAction(action.id, snapshot),
      })),
    };
  };

  const closeContextMenu = () => {
    contextMenu = null;
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
      title: t('desktop.contextMenu.actionFailedTitle'),
      message: t('desktop.contextMenu.actionFailedMessage'),
      variant: 'warn',
    });
  };

  const runContextMenuAction = async (actionId, snapshot) => {
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
    }
  };

  return {
    get contextMenu() {
      return contextMenu;
    },
    get handleContextMenu() {
      return handleContextMenu;
    },
    get closeContextMenu() {
      return closeContextMenu;
    },
  };
}
