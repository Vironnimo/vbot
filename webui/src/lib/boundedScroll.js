// A scroll box of bounded height for long content inside the Chat timeline
// (Tool output, diffs). While its content overflows, the box is focusable so
// keyboard users can scroll it, and it carries `data-overflowing` for
// styling. With `follow`, it keeps showing the end of growing content (live
// command output) until the reader scrolls away from the end; returning to
// the end resumes following.
//
// The box's first element child holds the content: its size changes, not the
// box's, when content grows past the height bound.

const END_TOLERANCE_PX = 4;

export function boundedScroll(node, { follow = false } = {}) {
  let following = follow;
  let atEnd = true;

  function atEndNow() {
    return (
      node.scrollHeight - node.scrollTop - node.clientHeight <= END_TOLERANCE_PX
    );
  }

  function refresh() {
    const overflowing = node.scrollHeight > node.clientHeight + 1;
    if (overflowing) {
      node.tabIndex = 0;
      node.setAttribute('data-overflowing', '');
    } else {
      node.removeAttribute('tabindex');
      node.removeAttribute('data-overflowing');
    }
    if (following && atEnd && overflowing) {
      node.scrollTop = node.scrollHeight;
    }
  }

  const handleScroll = () => {
    atEnd = atEndNow();
  };
  node.addEventListener('scroll', handleScroll, { passive: true });

  const observer =
    typeof ResizeObserver === 'function' ? new ResizeObserver(refresh) : null;
  observer?.observe(node);
  if (node.firstElementChild) observer?.observe(node.firstElementChild);
  refresh();

  return {
    update({ follow: nextFollow = false } = {}) {
      following = nextFollow;
      refresh();
    },
    destroy() {
      observer?.disconnect();
      node.removeEventListener('scroll', handleScroll);
    },
  };
}

// Whether `target`, inside the timeline `container`, sits in a nested scroll
// box that the input moves instead of the timeline: a box that can still
// scroll in that direction (`upward`) consumes the gesture.
export function nestedScrollConsumes(target, container, upward) {
  let element = target instanceof Element ? target : target?.parentElement;
  while (element && element !== container) {
    if (element.scrollHeight > element.clientHeight + 1) {
      const { overflowY } = getComputedStyle(element);
      if (overflowY === 'auto' || overflowY === 'scroll') {
        const canMove = upward
          ? element.scrollTop > 0
          : element.scrollHeight - element.scrollTop - element.clientHeight > 1;
        if (canMove) return true;
      }
    }
    element = element.parentElement;
  }
  return false;
}
