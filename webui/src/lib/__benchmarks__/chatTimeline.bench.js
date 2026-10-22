// @vitest-environment jsdom
// Client cost of applying the server's conversation projection. Semantic
// History/live reconstruction is measured at its server owner, not here.
// jsdom compiles runes for the client, so the Chat state is a real `$state`.
import { bench } from 'vitest';
import { ensureSessionState } from '../chatState.js';
import {
  applyTimelineEvent,
  loadTimelineItems,
  timelineRowsForViewport,
  visibleTimelineItemsForRender,
} from '../chatTimeline.js';
import {
  benchName,
  benchOptions,
  createRandom,
  markdownText,
  words,
} from './benchSupport.js';
import { reactiveChatState } from './chatState.support.svelte.js';

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

// The same patch where the app applies it: the displayed Session inside the
// Chat state's `$state` proxy, projected into viewport rows with every long
// Run split into parts. Unchanged Runs must cost nothing per patch.
const longRun = (id, count, status) => ({
  id,
  type: 'assistant_run',
  runId: id,
  status,
  items: Array.from({ length: count }, (_, position) =>
    position % 2
      ? {
          id: `${id}-tool-${position}`,
          type: 'tool_call',
          name: 'read',
          status: 'success',
          position,
          result: { ok: true, data: { content: words(random, 200) } },
        }
      : {
          id: `${id}-answer-${position}`,
          type: 'assistant_output',
          content: words(random, 300),
          streaming: false,
          position,
        },
  ),
});
const LONG_RUNS = 20;
const chatSession = ensureSessionState(reactiveChatState(), 'agent', 'bench');
loadTimelineItems(chatSession, [
  ...Array.from({ length: LONG_RUNS }, (_, index) =>
    longRun(`long-${index}`, 90, 'completed'),
  ),
]);
applyTimelineEvent(chatSession, 'timeline_snapshot', {
  run_id: 'live',
  sequence: 1,
  items: [longRun('live', 150, 'running')],
});
let chatSequence = 1;
bench(
  benchName('timeline.chat_state_patch[20x90+150children]', {
    rows: LONG_RUNS + 1,
  }),
  () => {
    const base = chatSequence++;
    applyTimelineEvent(chatSession, 'timeline_update', {
      run_id: 'live',
      base_sequence: base,
      sequence: chatSequence,
      run: { id: 'live' },
      child_updates: [{ id: 'live-answer-148', append: { content: ' word' } }],
    });
    timelineRowsForViewport(visibleTimelineItemsForRender(chatSession));
  },
  benchOptions(),
);
