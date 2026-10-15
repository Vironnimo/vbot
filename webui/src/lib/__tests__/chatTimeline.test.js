import { describe, expect, it } from 'vitest';
import {
  applyTimelineEvent,
  assistantRunChildProgressKey,
  loadTimelineItems,
  mergeTimelineItems,
  visibleTimelineItemsForRender,
} from '../chatTimeline.js';
import {
  assistantRun,
  messageItem,
  outputItem,
  reasoningItem,
  toolItem,
} from '../../components/__tests__/timelineItems.support.js';

const snapshot = (items, sequence = 10, runId = 'one') => ({
  run_id: runId,
  sequence,
  items,
});
const update = (fields, sequence = 11, baseSequence = 10) => ({
  run_id: 'one',
  sequence,
  base_sequence: baseSequence,
  ...fields,
});
const state = (items = []) => ({ timelineItems: items, timelineSequences: {} });

describe('server Timeline projection', () => {
  it('replaces only the selected Run in place and keeps unrelated rows unchanged', () => {
    const before = messageItem({
      id: 'old-user',
      role: 'user',
      content: 'Before',
    });
    const next = assistantRun('two', [outputItem('Later')]);
    const session = state([
      before,
      assistantRun('one', [outputItem('Draft')]),
      next,
    ]);
    const user = messageItem(
      { id: 'user', role: 'user', content: 'Hello' },
      { runId: 'one' },
    );
    const run = assistantRun('one', [outputItem('Answer')], {
      status: 'completed',
    });
    expect(
      applyTimelineEvent(session, 'timeline_snapshot', snapshot([user, run])),
    ).toBe(true);
    expect(session.timelineItems).toEqual([before, user, run, next]);
    expect(session.timelineItems[0]).toBe(before);
    expect(session.timelineItems[3]).toBe(next);
    expect(session.timelineSequences.one).toBe(10);
  });

  it('reuses unchanged rows and children across repeated authoritative snapshots', () => {
    const session = state();
    const frame = snapshot([
      assistantRun('one', [toolItem(), outputItem('Answer')]),
    ]);
    applyTimelineEvent(session, 'timeline_snapshot', frame);
    const run = session.timelineItems[0];
    applyTimelineEvent(session, 'timeline_snapshot', structuredClone(frame));
    expect(session.timelineItems[0]).toBe(run);
    expect(session.timelineItems[0].items[0]).toBe(run.items[0]);
  });

  it('appends only changed fields, preserves stable children and ignores duplicate or stale updates', () => {
    const session = state();
    applyTimelineEvent(
      session,
      'timeline_snapshot',
      snapshot([
        assistantRun('one', [
          toolItem(),
          outputItem('Hello', { streaming: true }),
        ]),
      ]),
    );
    const prior = session.timelineItems[0];
    const frame = update({
      run: { id: prior.id, iterationCount: 2 },
      child_updates: [
        {
          id: 'output-one',
          append: { content: ' world' },
          set: { streaming: false },
        },
      ],
    });
    expect(applyTimelineEvent(session, 'timeline_update', frame)).toBe(true);
    const run = session.timelineItems[0];
    expect(run.outputs[0]).toMatchObject({
      content: 'Hello world',
      streaming: false,
    });
    expect(run.tools[0]).toBe(prior.tools[0]);
    expect(prior.outputs[0].content).toBe('Hello');
    expect(applyTimelineEvent(session, 'timeline_update', frame)).toBe(false);
    expect(
      applyTimelineEvent(session, 'timeline_snapshot', snapshot([], 9)),
    ).toBe(false);
    expect(session.timelineItems[0]).toBe(run);
  });

  it('applies changed children, removals and explicit order atomically', () => {
    const session = state();
    applyTimelineEvent(
      session,
      'timeline_snapshot',
      snapshot([
        assistantRun('one', [
          reasoningItem('Draft'),
          toolItem(),
          outputItem('First'),
        ]),
      ]),
    );
    const prior = session.timelineItems[0];
    applyTimelineEvent(
      session,
      'timeline_update',
      update({
        run: { id: prior.id, status: 'completed' },
        remove_children: ['reasoning-one'],
        children: [outputItem('Final', { id: 'final' })],
        order: ['call-one', 'output-one', 'final'],
      }),
    );
    const run = session.timelineItems[0];
    expect(run.items.map((item) => item.id)).toEqual([
      'call-one',
      'output-one',
      'final',
    ]);
    expect(run.reasoning).toEqual([]);
    expect(run.outputs.map((item) => item.content)).toEqual(['First', 'Final']);
    expect(run.tools[0]).toBe(prior.tools[0]);
  });

  it('applies top-level upserts, removals and order without disturbing another Run', () => {
    const other = assistantRun('other', []);
    const session = state([other]);
    const first = messageItem(
      { id: 'first', content: 'One' },
      { runId: 'one' },
    );
    applyTimelineEvent(
      session,
      'timeline_snapshot',
      snapshot([first, assistantRun('one', [])]),
    );
    const correction = messageItem(
      { id: 'correction', content: 'Correction' },
      { runId: 'one' },
    );
    applyTimelineEvent(
      session,
      'timeline_update',
      update({
        remove: ['first'],
        upsert: [correction],
        item_order: ['correction', 'assistant-run-one'],
      }),
    );
    expect(session.timelineItems.map((item) => item.id)).toEqual([
      other.id,
      correction.id,
      'assistant-run-one',
    ]);
    expect(session.timelineItems[0]).toBe(other);
  });

  it.each([
    { run: { id: 'missing' } },
    {
      run: { id: 'assistant-run-one' },
      child_updates: [{ id: 'missing', append: { content: 'x' } }],
    },
    { item_order: ['missing'] },
    { base_sequence: 9, run: { id: 'assistant-run-one' } },
  ])(
    'rejects a patch without its matching base without partial application: %j',
    (invalid) => {
      const session = state();
      applyTimelineEvent(
        session,
        'timeline_snapshot',
        snapshot([assistantRun('one', [outputItem('Before')])]),
      );
      const prior = session.timelineItems;
      expect(
        applyTimelineEvent(
          session,
          'timeline_update',
          update({
            upsert: [messageItem({ id: 'must-not-appear' })],
            ...invalid,
          }),
        ),
      ).toBe(false);
      expect(session.timelineItems).toBe(prior);
      expect(session.timelineSequences.one).toBe(10);
    },
  );

  it('merges paged item identities without duplicating overlap or moving loaded rows', () => {
    const first = messageItem({ id: 'first' });
    const second = assistantRun('one', [outputItem('Current')]);
    const merged = mergeTimelineItems(
      [second],
      [first, structuredClone(second)],
      { prepend: true },
    );
    expect(merged).toEqual([first, second]);
    expect(merged[1]).toBe(second);
    expect(mergeTimelineItems(merged, [structuredClone(second)])[0]).toBe(
      first,
    );
  });

  it('keeps a newer live Run while applying a stale active-History page', () => {
    const session = state();
    applyTimelineEvent(
      session,
      'timeline_snapshot',
      snapshot([assistantRun('one', [outputItem('Fresh')])], 12),
    );
    const live = session.timelineItems[0];
    const older = assistantRun('old', [outputItem('Older')], {
      status: 'completed',
    });
    loadTimelineItems(
      session,
      [older, assistantRun('one', [outputItem('Stale')])],
      {
        activeRunId: 'one',
        timelineSequence: 8,
      },
    );
    expect(session.timelineItems).toEqual([older, live]);
    expect(session.timelineItems[1]).toBe(live);
  });

  it('displays standalone Compaction separators without hiding failed Run outcomes', () => {
    const separator = {
      id: 'compact',
      type: 'compaction_separator',
      status: 'completed',
    };
    const successful = assistantRun('one', [separator], {
      status: 'completed',
    });
    const failed = assistantRun('two', [separator], { status: 'failed' });
    expect(visibleTimelineItemsForRender(state([successful, failed]))).toEqual([
      separator,
      failed,
    ]);
  });

  it('changes the progress key for growing text, Tool arguments and settled state', () => {
    const child = outputItem('Hello', { streaming: true });
    const key = assistantRunChildProgressKey(child);
    expect(
      assistantRunChildProgressKey({ ...child, content: 'Hello world' }),
    ).not.toBe(key);
    expect(
      assistantRunChildProgressKey({ ...child, partialArgumentsText: '{}' }),
    ).not.toBe(key);
    expect(
      assistantRunChildProgressKey({ ...child, status: 'completed' }),
    ).not.toBe(key);
  });
});
