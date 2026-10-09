// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

import {
  appendEvents,
  flushSync,
  setupChatTimelineSuite,
  timelineSession,
  toolResult,
  toolStarted,
  waitForCondition,
} from './ChatTimeline.support.js';
import { reactiveProps } from './reactiveProps.support.svelte.js';
import { createChatState } from '../../lib/chatState.js';

// One Run whose Reasoning and two Tool calls form a compact Working group;
// the running bash call offers a cancel action.
function sessionWithWork(sessionId, runId, chatState) {
  const sessionState = timelineSession(sessionId, chatState);
  appendEvents(sessionState, runId, [
    {
      type: 'reasoning',
      payload: { message: { role: 'assistant', reasoning: 'Look first' } },
    },
    toolStarted('call-read', 'read', { path: 'notes.md' }),
    toolResult('call-read', 'read', { ok: true, data: { content: 'notes' } }),
    toolStarted('call-bash', 'bash', { command: 'sleep 60' }),
  ]);
  return sessionState;
}

function expand(selector) {
  const disclosure = document.querySelector(selector);
  disclosure.open = true;
  disclosure.dispatchEvent(new Event('toggle'));
  flushSync();
}

function rowState() {
  return {
    working: document.querySelector('.working-block').open,
    reasoning: document.querySelector('.reasoning-block')?.open ?? false,
    tool: document.querySelector('.run-tool-event')?.open ?? false,
    cancelling:
      document
        .querySelector('[data-cancel="tool"]')
        ?.getAttribute('aria-busy') === 'true',
  };
}

describe('ChatTimeline row state', () => {
  const timeline = setupChatTimelineSuite();

  it('keeps expanded rows and pending row actions of each Session across remounts', async () => {
    const chatState = createChatState();
    const first = sessionWithWork('session-first', 'run-first', chatState);
    const second = sessionWithWork('session-second', 'run-second', chatState);
    let finishCancel;
    const onCancelToolCall = vi.fn(
      () =>
        new Promise((resolve) => {
          finishCancel = resolve;
        }),
    );
    const props = reactiveProps({
      sessionState: first,
      agentName: 'Alpha',
      chatWorkingMode: 'compact',
      onCancelToolCall,
    });
    timeline.mount(props);

    expand('.working-block');
    expand('.reasoning-block');
    expand('.run-tool-event');
    document.querySelector('[data-cancel="tool"]').click();
    flushSync();
    const expanded = {
      working: true,
      reasoning: true,
      tool: true,
      cancelling: true,
    };
    expect(rowState()).toEqual(expanded);

    // The other Session's rows start collapsed; returning mounts the first
    // Session's rows again with their state.
    props.sessionState = second;
    flushSync();
    expect(rowState()).toEqual({
      working: false,
      reasoning: false,
      tool: false,
      cancelling: false,
    });
    props.sessionState = first;
    flushSync();
    expect(rowState()).toEqual(expanded);

    finishCancel();
    await waitForCondition(() => rowState().cancelling === false);
    expect(onCancelToolCall).toHaveBeenCalledTimes(1);
  });
});
