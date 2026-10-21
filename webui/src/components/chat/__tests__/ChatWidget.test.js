// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { fromStore, writable } from 'svelte/store';
import ChatWidget from '../ChatWidget.svelte';
import { init, t } from '../../../lib/i18n.js';
import {
  changedWidgetStates,
  reportWidgetState,
  resetWidgetStates,
  storedWidgetState,
} from '../../../lib/widgetStates.svelte.js';

vi.mock(
  'svelte',
  async () => import('../../../../node_modules/svelte/src/index-client.js'),
);
vi.mock(
  'svelte/store',
  async () =>
    import('../../../../node_modules/svelte/src/store/index-client.js'),
);

const { default: ChatWidgetHost } =
  await import('./ChatWidgetHost.support.svelte');

const SESSION = 'alpha::session-one';
const WIDGET = {
  id: 'w1',
  title: 'Typography',
  url: '/api/widgets/abc.def',
};

let component;

beforeEach(() => {
  init('en');
  localStorage.clear();
  resetWidgetStates();
});

afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  resetWidgetStates();
  localStorage.clear();
});

// Mounts the widget in a Chat timeline of SESSION; assigning
// `widget.current` changes the widget prop.
function mountWidget(initialWidget = WIDGET) {
  const widget = fromStore(writable(initialWidget));
  component = mount(ChatWidgetHost, {
    target: document.body,
    props: {
      sessionKey: SESSION,
      get widget() {
        return widget.current;
      },
    },
  });
  flushSync();
  return widget;
}

function frame() {
  return document.querySelector('iframe');
}

// Posts `data` to the parent as the frame's window unless `from` says
// otherwise.
function postFromFrame(data, { source, origin = 'null' } = {}) {
  window.dispatchEvent(
    new MessageEvent('message', {
      origin,
      source: source === undefined ? frame().contentWindow : source,
      data,
    }),
  );
  flushSync();
}

// Says hello as the frame's document; returns the init it received.
function connect() {
  const send = vi.spyOn(frame().contentWindow, 'postMessage');
  postFromFrame({ type: 'vbot-widget-hello' });
  const [message, targetOrigin] = send.mock.calls.at(-1) ?? [];
  send.mockRestore();
  return { message, targetOrigin };
}

describe('ChatWidget', () => {
  it('shows the widget document in a sandboxed frame without device access', () => {
    mountWidget();

    expect(document.querySelector('.chat-widget__title').textContent).toBe(
      'Typography',
    );
    expect(frame().getAttribute('src')).toBe(WIDGET.url);
    expect(frame().getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame().getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(frame().getAttribute('allow')).toBe(
      "camera 'none'; microphone 'none'; geolocation 'none'; clipboard-read 'none'; clipboard-write 'none'",
    );
  });

  it.each([
    ['without a stored state', null],
    ['with the stored state to restore', { line_height: 1.6 }],
  ])("answers the document's hello %s", (_label, stored) => {
    if (stored) {
      reportWidgetState(SESSION, WIDGET.id, { state: stored });
    }
    mountWidget();

    const { message, targetOrigin } = connect();

    expect(targetOrigin).toBe('*');
    expect(message).toEqual({
      type: 'vbot-widget-init',
      nonce: expect.stringMatching(/^[0-9a-f]{32}$/),
      state: stored,
    });
  });

  it('accepts reports only from its frame with the nonce it handed out', () => {
    mountWidget();
    const { nonce } = connect().message;
    const report = (state, extra = {}) => ({
      type: 'vbot-widget-state',
      nonce,
      state,
      initial: false,
      ...extra,
    });

    postFromFrame(report({ size: 1 }), { source: window });
    postFromFrame(report({ size: 2 }), { origin: 'https://example.com' });
    postFromFrame(report({ size: 3 }, { nonce: 'other' }));
    postFromFrame(report({ big: 'x'.repeat(9 * 1024) }));
    postFromFrame({ ...report({ size: 4 }), type: 'vbot-widget-unknown' });
    expect(storedWidgetState(SESSION, WIDGET.id)).toBeNull();

    postFromFrame(report({ size: 16 }, { initial: true }));
    postFromFrame(report({ size: 17 }));

    expect(changedWidgetStates(SESSION)).toEqual([
      expect.objectContaining({
        widgetId: WIDGET.id,
        title: 'Typography',
        state: { size: 17 },
      }),
    ]);
  });

  it.each([
    [5000, '720px'],
    [10, '48px'],
    [300.2, '301px'],
  ])('sizes the frame to a reported height of %s', (height, expected) => {
    mountWidget();
    const { nonce } = connect().message;

    postFromFrame({ type: 'vbot-widget-size', nonce, height });

    expect(frame().style.height).toBe(expected);
  });

  it('repeats the nonce for a repeated hello and hands a new document a new one', () => {
    mountWidget();
    const first = connect().message.nonce;

    expect(connect().message.nonce).toBe(first);

    postFromFrame({ type: 'vbot-widget-size', nonce: first, height: 200 });
    const second = connect().message.nonce;

    expect(second).not.toBe(first);
    postFromFrame({
      type: 'vbot-widget-state',
      nonce: first,
      state: { stale: true },
      initial: true,
    });
    expect(storedWidgetState(SESSION, WIDGET.id)).toBeNull();
  });

  it('follows a new document URL only until the frame connects', () => {
    const widget = mountWidget();
    const retired = frame().contentWindow;

    widget.current = { ...WIDGET, url: '/api/widgets/new.token' };
    flushSync();
    expect(frame().getAttribute('src')).toBe('/api/widgets/new.token');

    const { message } = connect();
    postFromFrame(
      {
        type: 'vbot-widget-state',
        nonce: message.nonce,
        state: { size: 1 },
        initial: true,
      },
      { source: retired },
    );
    expect(storedWidgetState(SESSION, WIDGET.id)).toBeNull();

    widget.current = { ...WIDGET, url: '/api/widgets/later.token' };
    flushSync();
    expect(frame().getAttribute('src')).toBe('/api/widgets/new.token');
  });

  it.each([
    [
      'a widget without a document',
      () => mountWidget({ ...WIDGET, url: '' }),
      () => t('chat.widget.unavailable'),
    ],
    [
      'a widget outside a Chat timeline',
      () => {
        component = mount(ChatWidget, {
          target: document.body,
          props: { widget: WIDGET },
        });
        flushSync();
      },
      () => t('chat.widget.chatOnly'),
    ],
  ])('shows a notice instead of the frame for %s', (_label, render, notice) => {
    render();

    expect(frame()).toBeNull();
    expect(document.querySelector('.chat-widget__notice').textContent).toBe(
      notice(),
    );
  });
});
