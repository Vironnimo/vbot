import { describe, expect, it } from 'vitest';
import { visibleTimelineItemsForRender } from '../chatTimeline.js';

const user = {
  id: 'u1',
  role: 'user',
  content: 'Original',
  history_run_id: 'r',
};
const before = {
  id: 'a1',
  role: 'assistant',
  content: 'Before',
  history_run_id: 'r',
};
const steer = {
  id: 'u2',
  role: 'user',
  content: 'Correction',
  history_run_id: 'r',
};
const after = {
  id: 'a2',
  role: 'assistant',
  content: 'After',
  history_run_id: 'r',
};
const events = [
  ['run_started', { status: 'running' }],
  ['user_message_persisted', { message: user }],
  ['assistant_output', { message: before }],
  ['user_message_persisted', { message: steer, queue_item_id: 'q' }],
  ['assistant_output', { message: after }],
].map(([type, payload], i) => ({
  type,
  payload,
  run_id: 'r',
  sequence: i + 1,
}));

describe('steering timeline', () => {
  for (const mode of ['live', 'mixed', 'history']) {
    it(`keeps user corrections between responses in ${mode}`, () => {
      const state = {
        messages: mode === 'live' ? [] : [user, before, steer, after],
        runEvents: mode === 'history' ? [] : events,
        streamingRunEvents: [],
        currentRun:
          mode === 'history' ? null : { runId: 'r', status: 'running' },
        historyRuns: {},
      };
      const timeline = visibleTimelineItemsForRender(state);
      const all = timeline.flatMap((item) =>
        item.type === 'assistant_run' ? item.items : [item],
      );
      expect(
        all.map(
          (item) =>
            item.message?.content ??
            item.event?.payload?.message?.content ??
            item.content,
        ),
      ).toEqual(['Original', 'Before', 'Correction', 'After']);
      expect(all.filter((item) => item.type === 'user_message')).toHaveLength(
        1,
      );
    });
  }
});
