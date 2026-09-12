// @vitest-environment jsdom
import { afterEach, beforeEach, vi } from 'vitest';
const { flushSync, mount, unmount } = await import('svelte');

import {
  appendRunEvent,
  createChatState,
  ensureSessionState,
} from '../../lib/chatState.js';
import { init } from '../../lib/i18n.js';

vi.mock('svelte', async () => {
  return import('../../../node_modules/svelte/src/index-client.js');
});

vi.mock('svelte/store', async () => {
  return import('../../../node_modules/svelte/src/store/index-client.js');
});

const { default: ChatTimeline } = await import('../ChatTimeline.svelte');

function setupTimelineToolSuite() {
  let mountedComponent;
  beforeEach(() => {
    document.body.innerHTML = '';
    init('en');
    mountedComponent = null;
  });
  afterEach(async () => {
    if (mountedComponent) {
      await unmount(mountedComponent);
      mountedComponent = null;
    }

    document.body.innerHTML = '';
    vi.useRealTimers();
  });
  // ---------------------------------------------------------------------------
  // compactToolValue unit tests (tested via rendered .teb-code elements)
  // ---------------------------------------------------------------------------
  function detailCodeText(detailRow) {
    const fields = Array.from(detailRow?.querySelectorAll('.teb-field') ?? []);
    if (fields.length > 0) {
      return fields
        .map((field) => {
          const key = field.querySelector('.teb-field-key')?.textContent ?? '';
          const value =
            field.querySelector('.teb-field-value')?.textContent ?? '';
          return `${key}: ${value}`;
        })
        .join('\n');
    }
    return detailRow?.querySelector('.teb-code')?.textContent ?? '';
  }
  /**
   * Mounts a single tool_call_result event and returns the text content of the
   * Result `.teb-code` element.  `resultValue` is placed verbatim into
   * payload.result (preferPayload=true path).
   */
  function getResultCodeText(resultValue, sessionId, toolName = 'probe') {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      sessionId,
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: `run-${sessionId}`,
      sequence: 1,
      payload: {
        tool_call: {
          id: `call-${sessionId}`,
          index: 0,
          name: toolName,
          arguments: {},
        },
      },
    });
    appendRunEvent(sessionState, {
      type: 'tool_call_result',
      run_id: `run-${sessionId}`,
      sequence: 2,
      payload: {
        tool_call: { id: `call-${sessionId}`, index: 0, name: toolName },
        result: resultValue,
      },
    });
    const comp = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();
    const tebRows = document.querySelectorAll('.teb-row');
    const resultRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Result',
    );
    const text = detailCodeText(resultRow);
    unmount(comp);
    document.body.innerHTML = '';
    return text;
  }
  /**
   * Mounts a single tool_call_started event and returns the Args `.teb-code`
   * text (preferPayload=false path).
   */
  function getArgsCodeText(argsValue, sessionId) {
    const sessionState = ensureSessionState(
      createChatState(),
      'alpha',
      sessionId,
    );
    appendRunEvent(sessionState, {
      type: 'tool_call_started',
      run_id: `run-${sessionId}`,
      sequence: 1,
      payload: {
        tool_call: {
          id: `call-${sessionId}`,
          index: 0,
          name: 'probe',
          arguments: argsValue,
        },
      },
    });
    const comp = mount(ChatTimeline, {
      target: document.body,
      props: { sessionState, agentName: 'Alpha' },
    });
    flushSync();
    const tebRows = document.querySelectorAll('.teb-row');
    const argsRow = Array.from(tebRows).find(
      (el) => el.querySelector('.teb-label')?.textContent === 'Args',
    );
    const text = detailCodeText(argsRow);
    unmount(comp);
    document.body.innerHTML = '';
    return text;
  }
  return {
    get mountedComponent() {
      return mountedComponent;
    },
    set mountedComponent(value) {
      mountedComponent = value;
    },
    detailCodeText,
    getResultCodeText,
    getArgsCodeText,
  };
}

export { ChatTimeline, setupTimelineToolSuite };

export { flushSync, mount, unmount };
