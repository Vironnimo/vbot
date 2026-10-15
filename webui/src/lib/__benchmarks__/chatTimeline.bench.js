// Client cost of applying the server's conversation projection. Semantic
// History/live reconstruction is measured at its server owner, not here.
import { bench } from 'vitest';
import {
  applyTimelineEvent,
  loadTimelineItems,
  visibleTimelineItemsForRender,
} from '../chatTimeline.js';
import {
  benchName,
  benchOptions,
  createRandom,
  markdownText,
  words,
} from './benchSupport.js';

const random = createRandom(1000);
const RUNS = 500;
const items = Array.from({ length: RUNS }, (_, index) => [
  {
    id: `user-${index}`,
    type: 'message',
    runId: `run-${index}`,
    message: { id: `user-${index}`, role: 'user', content: words(random, 200) },
  },
  {
    id: `run-${index}`,
    type: 'assistant_run',
    runId: `run-${index}`,
    source: 'history',
    status: 'completed',
    items: [
      {
        id: `tool-${index}`,
        type: 'tool_call',
        name: 'read',
        status: 'success',
        result: { ok: true, data: { content: words(random, 400) } },
      },
      {
        id: `answer-${index}`,
        type: 'assistant_output',
        content: markdownText(random, 1000),
        streaming: false,
      },
    ],
  },
]).flat();
const idle = { timelineItems: [], timelineSequences: {} };
loadTimelineItems(idle, items);
bench(
  benchName('timeline.history[1000rows]', { rows: items.length }),
  () => visibleTimelineItemsForRender(idle),
  benchOptions(),
);

const reloads = [structuredClone(items), structuredClone(items)];
let reloadIndex = 0;
bench(
  benchName('timeline.history_reload[1000rows]', { rows: items.length }),
  () => {
    reloadIndex = 1 - reloadIndex;
    loadTimelineItems(idle, reloads[reloadIndex]);
    visibleTimelineItemsForRender(idle);
  },
  benchOptions(),
);

const streaming = {
  timelineItems: [...idle.timelineItems],
  timelineSequences: {},
};
applyTimelineEvent(streaming, 'timeline_snapshot', {
  run_id: 'active',
  sequence: 1,
  items: [
    {
      id: 'active',
      runId: 'active',
      type: 'assistant_run',
      status: 'running',
      items: [
        {
          id: 'answer-active',
          type: 'assistant_output',
          content: words(random, 8000),
          streaming: true,
        },
      ],
    },
  ],
});
let sequence = 1;
const output = [words(random, 8000), words(random, 8040)];
bench(
  benchName('timeline.streaming_patch[1000rows]', { rows: items.length + 1 }),
  () => {
    const base = sequence++;
    applyTimelineEvent(streaming, 'timeline_update', {
      run_id: 'active',
      base_sequence: base,
      sequence,
      run: { id: 'active' },
      child_updates: [
        { id: 'answer-active', set: { content: output[sequence % 2] } },
      ],
    });
    visibleTimelineItemsForRender(streaming);
  },
  benchOptions(),
);
