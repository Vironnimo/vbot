const TOAST_VARIANTS = Object.freeze(['error', 'warn', 'info', 'success']);

let nextToastId = 1;

function normalizeVariant(variant) {
  return TOAST_VARIANTS.includes(variant) ? variant : 'info';
}

export function createToastState() {
  return { toasts: [] };
}

export function addToast(state, { title, message, variant }) {
  const id = `toast-${nextToastId}`;
  state.toasts.push({
    id,
    title,
    message,
    variant: normalizeVariant(variant),
  });
  nextToastId += 1;
  return id;
}

export function dismissToast(state, id) {
  state.toasts = state.toasts.filter((toast) => toast.id !== id);
}
