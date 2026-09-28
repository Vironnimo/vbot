// Fake block layout for the Chat timeline in jsdom, which has none. The
// scroller shows a `viewportHeight` slice of its content column; the column's
// children stack from its top: rows with the height `rowHeight(element)`
// returns, spacers with their inline height, everything else with none. The
// scroller clamps scrollTop to its content like a browser. A ResizeObserver
// fake reports observed elements whose height changed since their last
// report, as a browser does after layout. Animation frames run only when the
// test lets the layout settle.
import { flushSync, tick } from 'svelte';

const PATCHES = [
  [Element.prototype, 'getBoundingClientRect'],
  [Element.prototype, 'clientHeight'],
  [Element.prototype, 'scrollHeight'],
  [Element.prototype, 'scrollTop'],
  [HTMLElement.prototype, 'offsetHeight'],
  [HTMLElement.prototype, 'offsetTop'],
];

export function installFakeLayout({
  viewportHeight = 500,
  rowHeight = () => 100,
} = {}) {
  const originals = PATCHES.map(([target, name]) => [
    target,
    name,
    Object.getOwnPropertyDescriptor(target, name),
  ]);
  const observers = new Set();
  let scrollTop = 0;
  const frames = new Map();
  let nextFrameId = 1;
  const originalFrameApi = {
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
  };
  globalThis.requestAnimationFrame = (callback) => {
    const id = nextFrameId;
    nextFrameId += 1;
    frames.set(id, callback);
    return id;
  };
  globalThis.cancelAnimationFrame = (id) => {
    frames.delete(id);
  };

  function runFrame() {
    const callbacks = Array.from(frames.values());
    frames.clear();
    for (const callback of callbacks) callback(performance.now());
  }

  const scroller = () => document.querySelector('.messages');
  const column = () => document.querySelector('.messages__content');

  function childHeight(element) {
    if (element.dataset.timelineSpacer !== undefined) {
      return Number.parseFloat(element.style.height) || 0;
    }
    if (element.dataset.timelineItemId !== undefined) {
      return rowHeight(element);
    }
    return 0;
  }

  function flow() {
    const boxes = new Map();
    let height = 0;
    for (const child of column()?.children ?? []) {
      const childBoxHeight = childHeight(child);
      boxes.set(child, { top: height, height: childBoxHeight });
      height += childBoxHeight;
    }
    return { boxes, height: Math.max(viewportHeight, height) };
  }

  function currentScrollTop() {
    scrollTop = Math.max(
      0,
      Math.min(scrollTop, flow().height - viewportHeight),
    );
    return scrollTop;
  }

  // The element's box relative to the scroller's top edge (the viewport), or
  // null for elements without fake layout.
  function box(element) {
    if (element === scroller()) {
      return { top: 0, height: viewportHeight };
    }
    const content = column();
    if (element === content) {
      return { top: -currentScrollTop(), height: flow().height };
    }
    if (content && element.parentElement === content) {
      const childBox = flow().boxes.get(element);
      return {
        top: childBox.top - currentScrollTop(),
        height: childBox.height,
      };
    }
    return null;
  }

  function override(target, name, descriptor) {
    Object.defineProperty(target, name, { configurable: true, ...descriptor });
  }

  function originalGet(target, name, element) {
    const original = originals.find(
      ([owner, property]) => owner === target && property === name,
    )[2];
    return original.get.call(element);
  }

  override(Element.prototype, 'getBoundingClientRect', {
    value() {
      const elementBox = box(this);
      if (!elementBox) {
        return {
          top: 0,
          bottom: 0,
          left: 0,
          right: 0,
          width: 0,
          height: 0,
        };
      }
      return {
        top: elementBox.top,
        bottom: elementBox.top + elementBox.height,
        left: 0,
        right: 600,
        width: 600,
        height: elementBox.height,
      };
    },
  });
  override(Element.prototype, 'clientHeight', {
    get() {
      return this === scroller()
        ? viewportHeight
        : originalGet(Element.prototype, 'clientHeight', this);
    },
  });
  override(Element.prototype, 'scrollHeight', {
    get() {
      return this === scroller()
        ? flow().height
        : originalGet(Element.prototype, 'scrollHeight', this);
    },
  });
  override(Element.prototype, 'scrollTop', {
    get() {
      return this === scroller()
        ? currentScrollTop()
        : originalGet(Element.prototype, 'scrollTop', this);
    },
    set(value) {
      if (this === scroller()) {
        scrollTop = value;
        currentScrollTop();
      }
    },
  });
  override(HTMLElement.prototype, 'offsetHeight', {
    get() {
      return box(this)?.height ?? 0;
    },
  });
  override(HTMLElement.prototype, 'offsetTop', {
    get() {
      const elementBox = box(this);
      return elementBox ? elementBox.top + currentScrollTop() : 0;
    },
  });

  globalThis.ResizeObserver = class {
    constructor(callback) {
      this.callback = callback;
      // target -> last reported height
      this.targets = new Map();
      observers.add(this);
    }

    observe(target) {
      this.targets.set(target, null);
    }

    unobserve(target) {
      this.targets.delete(target);
    }

    disconnect() {
      this.targets.clear();
      observers.delete(this);
    }
  };

  function deliverResizes() {
    for (const observer of observers) {
      const entries = [];
      for (const [target, reported] of observer.targets) {
        const height = target.isConnected ? (box(target)?.height ?? 0) : 0;
        if (height !== reported) {
          observer.targets.set(target, height);
          entries.push({ target, borderBoxSize: [{ blockSize: height }] });
        }
      }
      if (entries.length > 0) {
        observer.callback(entries, observer);
      }
    }
  }

  return {
    // Lets Svelte, the scroll controller, and resize reports run to rest,
    // one animation frame per round.
    async settle(rounds = 4) {
      for (let round = 0; round < rounds; round += 1) {
        await tick();
        flushSync();
        deliverResizes();
        runFrame();
        flushSync();
      }
    },
    // A user scroll: upward input first, then the scroll event.
    scrollTo(top, { upward = top < currentScrollTop() } = {}) {
      const container = scroller();
      if (upward) {
        container.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
      }
      scrollTop = top;
      container.dispatchEvent(new Event('scroll'));
    },
    scrollTop: currentScrollTop,
    maxScrollTop: () => flow().height - viewportHeight,
    // The row's top edge relative to the viewport, or null when unmounted.
    rowTop(id) {
      const row = mountedRow(id);
      return row ? box(row).top : null;
    },
    // The first row visible in the viewport, or null when it shows none.
    firstVisibleRowId() {
      for (const element of column()?.children ?? []) {
        const id = element.dataset.timelineItemId;
        const elementBox = box(element);
        if (
          id !== undefined &&
          elementBox.top + elementBox.height > 0 &&
          elementBox.top < viewportHeight
        ) {
          return id;
        }
      }
      return null;
    },
    uninstall() {
      for (const [target, name, descriptor] of originals) {
        Object.defineProperty(target, name, descriptor);
      }
      delete globalThis.ResizeObserver;
      Object.assign(globalThis, originalFrameApi);
      observers.clear();
    },
  };
}

export function mountedRow(id) {
  return document.querySelector(`[data-timeline-item-id="${id}"]`);
}

export function mountedRowIds() {
  return Array.from(
    document.querySelectorAll('.messages__content > [data-timeline-item-id]'),
    (row) => row.dataset.timelineItemId,
  );
}
