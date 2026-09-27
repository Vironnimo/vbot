// Floating-layer geometry: placement against an anchor and the pointer
// corridor that lets the pointer travel between an anchor and its layer.

export const ANCHOR_OFFSET = 6;
const EDGE_PADDING = 8;
// Half the side of the square around the exit point that seeds a corridor,
// so a pointer leaving at a steep angle still counts as travelling across.
const CORRIDOR_EXIT_PADDING = 4;

const PLACEMENTS = new Set(['top', 'right', 'bottom', 'left']);
const OPPOSITE = { top: 'bottom', bottom: 'top', left: 'right', right: 'left' };

/** Valid placements are top (default), right, bottom and left. */
export function normalizePlacement(value) {
  return PLACEMENTS.has(value) ? value : 'top';
}

function clamp(value, min, max) {
  return Math.min(Math.max(min, value), Math.max(min, max));
}

function fits(side, rect, width, height) {
  switch (side) {
    case 'top':
      return rect.top - ANCHOR_OFFSET - height >= EDGE_PADDING;
    case 'bottom':
      return (
        rect.bottom + ANCHOR_OFFSET + height <=
        window.innerHeight - EDGE_PADDING
      );
    case 'right':
      return (
        rect.right + ANCHOR_OFFSET + width <= window.innerWidth - EDGE_PADDING
      );
    default:
      return rect.left - ANCHOR_OFFSET - width >= EDGE_PADDING;
  }
}

function chooseSide(preferred, rect, width, height) {
  const candidates = [preferred, OPPOSITE[preferred], 'top', 'bottom'];
  const side = candidates.find((candidate) =>
    fits(candidate, rect, width, height),
  );
  if (side) {
    return side;
  }
  // Nothing fits: take the roomier vertical side and clamp to the viewport.
  return rect.top >= window.innerHeight - rect.bottom ? 'top' : 'bottom';
}

/**
 * Position `element` (position: fixed, already measurable) against `anchor`
 * on the preferred side, flipping to the opposite side and then above/below
 * when it does not fit, always clamped to the viewport. Top and bottom
 * placements center horizontally on the anchor, side placements vertically.
 * The chosen side is exposed as `data-floating-side` so the entry motion
 * starts from the anchor.
 */
export function positionFloating(anchor, element, placement = 'top') {
  const rect = anchor.getBoundingClientRect();
  const width = element.offsetWidth;
  const height = element.offsetHeight;
  const side = chooseSide(normalizePlacement(placement), rect, width, height);

  let left;
  let top;
  if (side === 'left' || side === 'right') {
    left =
      side === 'right'
        ? rect.right + ANCHOR_OFFSET
        : rect.left - ANCHOR_OFFSET - width;
    top = clamp(
      rect.top + rect.height / 2 - height / 2,
      EDGE_PADDING,
      window.innerHeight - height - EDGE_PADDING,
    );
  } else {
    left = clamp(
      rect.left + rect.width / 2 - width / 2,
      EDGE_PADDING,
      window.innerWidth - width - EDGE_PADDING,
    );
    top =
      side === 'top'
        ? Math.max(EDGE_PADDING, rect.top - ANCHOR_OFFSET - height)
        : clamp(
            rect.bottom + ANCHOR_OFFSET,
            EDGE_PADDING,
            window.innerHeight - height - EDGE_PADDING,
          );
  }

  element.style.left = `${left}px`;
  element.style.top = `${top}px`;
  element.dataset.floatingSide = side;
}

function cross(origin, a, b) {
  return (
    (a.x - origin.x) * (b.y - origin.y) - (a.y - origin.y) * (b.x - origin.x)
  );
}

// Andrew's monotone chain; returns the hull counter-clockwise.
function convexHull(points) {
  const sorted = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  const lower = [];
  for (const point of sorted) {
    while (lower.length >= 2 && cross(lower.at(-2), lower.at(-1), point) <= 0) {
      lower.pop();
    }
    lower.push(point);
  }
  const upper = [];
  for (const point of sorted.reverse()) {
    while (upper.length >= 2 && cross(upper.at(-2), upper.at(-1), point) <= 0) {
      upper.pop();
    }
    upper.push(point);
  }
  lower.pop();
  upper.pop();
  return lower.concat(upper);
}

/**
 * The region the pointer may cross after leaving one hover region at `exit`
 * on its way to `targetRect`: the convex hull of a small square around the
 * exit point and the target's corners, so it covers the gap and the
 * target itself.
 */
export function pointerCorridor(exit, targetRect) {
  const pad = CORRIDOR_EXIT_PADDING;
  return convexHull([
    { x: exit.x - pad, y: exit.y - pad },
    { x: exit.x + pad, y: exit.y - pad },
    { x: exit.x - pad, y: exit.y + pad },
    { x: exit.x + pad, y: exit.y + pad },
    { x: targetRect.left, y: targetRect.top },
    { x: targetRect.right, y: targetRect.top },
    { x: targetRect.left, y: targetRect.bottom },
    { x: targetRect.right, y: targetRect.bottom },
  ]);
}

/** True when `point` lies inside or on the edge of the convex `polygon`. */
export function pointInCorridor(point, polygon) {
  if (polygon.length < 3) {
    return false;
  }
  for (let index = 0; index < polygon.length; index += 1) {
    const next = polygon[(index + 1) % polygon.length];
    if (cross(polygon[index], next, point) < 0) {
      return false;
    }
  }
  return true;
}
