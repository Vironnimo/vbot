// Shared DOM and event helpers for the floating-layer tests of lib/tooltip.js.

function pointerEvent(type, pointerType) {
  const event = new Event(type, { bubbles: type === 'pointerdown' });
  Object.defineProperty(event, 'pointerType', { value: pointerType });
  return event;
}

// Keyboard modality: the next focus counts as keyboard focus.
function pressKey(target, key, options = {}) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  target.dispatchEvent(event);
  return event;
}

// A pointer event at viewport coordinates, as real browsers deliver them.
function pointerAt(type, x, y, { relatedTarget, buttons } = {}) {
  const event = new Event(type, { bubbles: type === 'pointerdown' });
  Object.defineProperty(event, 'clientX', { value: x });
  Object.defineProperty(event, 'clientY', { value: y });
  if (relatedTarget !== undefined) {
    Object.defineProperty(event, 'relatedTarget', { value: relatedTarget });
  }
  if (buttons) {
    Object.defineProperty(event, 'buttons', { value: buttons });
  }
  return event;
}

function placeAt(element, { left, top, width, height }) {
  element.getBoundingClientRect = () => ({
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
  });
}

function button(label, parent = document.body) {
  const element = document.createElement('button');
  element.type = 'button';
  element.textContent = label;
  parent.appendChild(element);
  return element;
}

export { button, placeAt, pointerAt, pointerEvent, pressKey };
