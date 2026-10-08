// A reading point inside one timeline row. The outer row still owns windowing;
// this locator holds a visible paragraph/line when that row reflows internally.
// Only capture scans the row. Corrections resolve one block, including after
// Markdown replaces its HTML or a Session's rows mount again.
const BLOCKS =
  'p, pre, li, td, th, h1, h2, h3, h4, h5, h6, summary, img, video, audio, .msg-body-text';
const CONTENT_SELECTOR = '[data-timeline-content-id]';

function elementPath(root, element) {
  const path = [];
  while (element && element !== root) {
    const parent = element.parentElement;
    if (!parent) return null;
    path.unshift(Array.prototype.indexOf.call(parent.children, element));
    element = parent;
  }
  return element === root ? path : null;
}

function elementAt(root, path) {
  for (const index of path) root = root?.children[index];
  return root;
}

function scopeFor(row, element) {
  const scope = element.closest(CONTENT_SELECTOR);
  return scope && row.contains(scope) ? scope : row;
}

function nestedScrollBox(row, element) {
  let box = null;
  for (let node = element; node && node !== row; node = node.parentElement) {
    // Include a box before it overflows: streaming may make it scrollable
    // after this reading point was captured.
    const { overflowY } = node.ownerDocument.defaultView.getComputedStyle(node);
    if (overflowY === 'auto' || overflowY === 'scroll') box = node;
  }
  return box;
}

function textRange(node, offset) {
  if (node?.nodeType !== 3 || offset < 0 || offset >= node.length) return null;
  const range = node.ownerDocument.createRange();
  range.setStart(node, offset);
  range.setEnd(node, offset + 1);
  const rect = range.getBoundingClientRect?.();
  return rect?.height > 0 ? rect : null;
}

function caretAt(document, x, y) {
  if (document.caretPositionFromPoint) {
    const caret = document.caretPositionFromPoint(x, y);
    return caret && { node: caret.offsetNode, offset: caret.offset };
  }
  const caret = document.caretRangeFromPoint?.(x, y);
  return caret && { node: caret.startContainer, offset: caret.startOffset };
}

function textPoint(element, bounds) {
  const rect = element.getBoundingClientRect();
  const y = Math.max(rect.top, bounds.top) + 2;
  for (const x of [rect.left + 4, (rect.left + rect.right) / 2]) {
    const caret = caretAt(element.ownerDocument, x, y);
    if (caret?.node?.nodeType !== 3 || !element.contains(caret.node)) continue;
    const offset = Math.min(caret.offset, caret.node.length - 1);
    const line = textRange(caret.node, offset);
    if (line && line.bottom > bounds.top && line.top < bounds.bottom) {
      return { node: caret.node, offset, top: line.top };
    }
  }
  return null;
}

function textOffset(block, node, offset) {
  const range = block.ownerDocument.createRange();
  range.selectNodeContents(block);
  range.setEnd(node, offset);
  return range.toString().length;
}

export function captureReadingAnchor(row, bounds) {
  if (!row.querySelectorAll || !row.ownerDocument) return null;
  let fallback = null;
  for (const element of row.querySelectorAll(BLOCKS)) {
    const rect = element.getBoundingClientRect();
    if (
      rect.height <= 0 ||
      rect.bottom <= bounds.top ||
      rect.top >= bounds.bottom
    ) {
      continue;
    }
    // A nested output/table scrollport owns its own reading position. Its
    // moving text must never make the outer timeline counter-scroll.
    const scrollBox = nestedScrollBox(row, element);
    if (scrollBox) {
      const scope = scopeFor(row, scrollBox);
      return {
        scopeId: scope === row ? null : scope.dataset.timelineContentId,
        path: elementPath(scope, scrollBox),
        delta: scrollBox.getBoundingClientRect().top - bounds.top,
      };
    }
    const scope = scopeFor(row, element);
    const point = textPoint(element, bounds);
    // The innermost text block keeps sibling text (headers, Tool status and
    // code-copy controls) out of the character offset.
    const block = point?.node.parentElement.closest(BLOCKS) ?? element;
    const path = elementPath(scope, block);
    if (!path) continue;
    const anchor = {
      scopeId: scope === row ? null : scope.dataset.timelineContentId,
      path,
      delta: (point?.top ?? rect.top) - bounds.top,
    };
    if (point) {
      const offset = textOffset(block, point.node, point.offset);
      return {
        ...anchor,
        offset,
        sample: block.textContent.slice(offset, offset + 24),
      };
    }
    fallback ??= anchor;
  }
  return fallback;
}

export function readingAnchorTop(row, saved) {
  if (!saved) return null;
  const scope =
    saved.scopeId === null
      ? row
      : Array.from(row.querySelectorAll(CONTENT_SELECTOR)).find(
          (element) => element.dataset.timelineContentId === saved.scopeId,
        );
  const block = elementAt(scope, saved.path);
  if (!block) return null;
  if (saved.offset === undefined) {
    const rect = block.getBoundingClientRect();
    return rect.height > 0 ? rect.top - saved.delta : null;
  }
  if (
    block.textContent.slice(
      saved.offset,
      saved.offset + saved.sample.length,
    ) !== saved.sample
  ) {
    return null;
  }
  // Markdown may split/merge inline nodes while streaming; a text offset in
  // this block survives that without searching or retaining the whole Run.
  const walker = block.ownerDocument.createTreeWalker(block, 4 /* SHOW_TEXT */);
  let offset = saved.offset;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (offset < node.length) {
      const rect = textRange(node, offset);
      return rect ? rect.top - saved.delta : null;
    }
    offset -= node.length;
  }
  return null;
}
