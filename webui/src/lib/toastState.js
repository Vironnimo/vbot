const TOAST_VARIANTS = Object.freeze(['error', 'warn', 'info', 'success']);

let nextToastId = 1;

function normalizeVariant(variant) {
  return TOAST_VARIANTS.includes(variant) ? variant : 'info';
}

export function createToastState() {
  return { toasts: [] };
}

export function addToast(state, { title, message, variant, key, actions }) {
  const existing =
    key == null ? null : state.toasts.find((toast) => toast.key === key);
  const id = existing?.id ?? `toast-${nextToastId++}`;
  const toast = {
    id,
    title,
    message,
    variant: normalizeVariant(variant),
    ...(key == null ? {} : { key }),
    ...(actions?.length ? { actions } : {}),
  };
  if (existing)
    state.toasts = state.toasts.map((item) => (item.id === id ? toast : item));
  else state.toasts.push(toast);
  return id;
}

export function dismissToast(state, id) {
  state.toasts = state.toasts.filter((toast) => toast.id !== id);
}
