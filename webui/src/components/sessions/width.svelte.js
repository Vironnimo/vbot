// The Session list's width: one preference every Chat area's drawer shares,
// so resizing one list resizes the other's too. Dragging updates it live;
// `save` persists it once the change is complete.
const STORAGE_KEY = 'vbot.chat.sessionListWidth';

export const SESSION_LIST_WIDTH = { min: 220, default: 260, max: 480 };

const clampWidth = (value) =>
  Math.round(
    Math.max(SESSION_LIST_WIDTH.min, Math.min(SESSION_LIST_WIDTH.max, value)),
  );

function loadWidth() {
  try {
    const stored = Number(localStorage.getItem(STORAGE_KEY));
    return stored > 0 ? clampWidth(stored) : SESSION_LIST_WIDTH.default;
  } catch {
    // Missing or unavailable storage leaves the default width.
    return SESSION_LIST_WIDTH.default;
  }
}

let width = $state(null);

export const sessionListWidth = {
  get value() {
    width ??= loadWidth();
    return width;
  },
  set(next) {
    if (Number.isFinite(next)) {
      width = clampWidth(next);
    }
  },
  save() {
    try {
      localStorage.setItem(STORAGE_KEY, String(this.value));
    } catch {
      // Storage is optional; the width stays usable in memory.
    }
  },
};
