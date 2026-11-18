// Report coordinates in the page's viewport. The opaque frame never receives
// a DOM reference to the Session or any of its messages.
function slotBounds(element, header) {
  const rect = element.getBoundingClientRect();
  const viewport = document.documentElement;
  let left = Math.max(0, rect.left);
  let top = Math.max(0, rect.top);
  let right = Math.min(viewport.clientWidth, rect.right);
  let bottom = Math.min(viewport.clientHeight, rect.bottom);
  for (let node = element.parentElement; node; node = node.parentElement) {
    const style = getComputedStyle(node);
    const parent = node.getBoundingClientRect();
    if (style.overflowX && style.overflowX !== 'visible') {
      left = Math.max(left, parent.left + node.clientLeft);
      right = Math.min(right, parent.left + node.clientLeft + node.clientWidth);
    }
    if (style.overflowY && style.overflowY !== 'visible') {
      top = Math.max(top, parent.top + node.clientTop);
      bottom = Math.min(
        bottom,
        parent.top + node.clientTop + node.clientHeight,
      );
    }
  }
  if (header) {
    const box = header.getBoundingClientRect();
    if (box.left < right && box.right > left) top = Math.max(top, box.bottom);
  }
  if (left >= right || top >= bottom) return null;
  return {
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
    clip: [
      top - rect.top,
      rect.right - right,
      rect.bottom - bottom,
      left - rect.left,
    ].map((inset) => Math.max(0, Math.round(inset))),
  };
}

export function observeSessionSlot(element, publish) {
  let last = '';
  let scheduled = 0;
  let header = null;
  function measure() {
    scheduled = 0;
    const id = element.dataset.sessionClipBelow;
    const next = id ? element.ownerDocument.getElementById(id) : null;
    if (next !== header) {
      if (header) sizes?.unobserve(header);
      header = next;
      if (header) sizes?.observe(header);
    }
    const bounds = slotBounds(element, header);
    const key = JSON.stringify(bounds);
    if (key !== last) {
      last = key;
      publish(bounds);
    }
  }
  function schedule() {
    if (!scheduled) scheduled = requestAnimationFrame(measure);
  }
  // A sibling can move the slot without resizing it. Changes coalesce with
  // nested scrolling, image loads, viewport resizing and disclosure changes.
  const mutations = new MutationObserver(schedule);
  mutations.observe(document.body, {
    attributes: true,
    subtree: true,
    childList: true,
  });
  const sizes =
    typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
  for (let node = element; node; node = node.parentElement)
    sizes?.observe(node);
  document.addEventListener('scroll', schedule, true);
  document.addEventListener('load', schedule, true);
  window.addEventListener('resize', schedule);
  measure();
  return () => {
    mutations.disconnect();
    sizes?.disconnect();
    document.removeEventListener('scroll', schedule, true);
    document.removeEventListener('load', schedule, true);
    window.removeEventListener('resize', schedule);
    cancelAnimationFrame(scheduled);
  };
}
