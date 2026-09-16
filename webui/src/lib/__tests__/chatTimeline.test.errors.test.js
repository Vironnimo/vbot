import { describe, expect, it } from 'vitest';
import { visibleTimelineItemsForRender } from '../chatTimeline.js';

const runId = 'test-failed-run';
const error = 'test-terminal-failure';
const failure = {
  type: 'run_failed',
  run_id: runId,
  sequence: 2,
  payload: { status: 'failed', error },
};
const message = { id: 'test-error', role: 'error', content: error };
const errorEvent = {
  type: 'error_message_persisted',
  run_id: runId,
  sequence: 1,
  payload: { message },
};
const summary = {
  id: 'test-summary',
  role: 'run_summary',
  run_id: runId,
  status: 'failed',
};
const errors = (state) =>
  visibleTimelineItemsForRender(state).filter(
    (item) => item.message?.role === 'error',
  );

describe('Run failure timeline fallback', () => {
  it('replaces the fallback when the canonical error event arrives', () => {
    const state = { messages: [], runEvents: [failure] };
    expect(errors(state).map((item) => item.message.content)).toEqual([error]);
    state.runEvents.push(errorEvent);
    expect(errors(state).map((item) => item.message)).toEqual([message]);
  });

  it.each([false, true])(
    'uses loaded canonical History once with a User anchor: %s',
    (withUser) => {
      const user = { id: 'test-user', role: 'user', content: 'test-request' };
      const state = {
        status: 'failed',
        currentRun: { runId, status: 'failed' },
        messages: withUser ? [user, summary, message] : [summary, message],
        runEvents: [
          failure,
          ...(withUser
            ? [
                {
                  type: 'user_message_persisted',
                  run_id: runId,
                  sequence: 0,
                  payload: { message: user },
                },
              ]
            : []),
        ],
      };
      expect(errors(state).map((item) => item.message)).toEqual([message]);
    },
  );

  it('keeps an earlier identical error from hiding a new failure', () => {
    const state = { messages: [message], runEvents: [failure] };
    expect(errors(state).map((item) => item.message.content)).toEqual([
      error,
      error,
    ]);
  });
});
