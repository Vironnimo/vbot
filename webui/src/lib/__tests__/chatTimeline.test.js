import { describe, expect, it } from 'vitest';
import {
  applyTimelineEvent,
  assistantRunChildProgressKey,
  loadTimelineItems,
  mergeTimelineItems,
  visibleTimelineItemsForRender,
  timelineRowsForViewport,
  mergeRunChildPage,
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
  it('keeps loaded Run prefixes through windowed reconnects and sparse updates', () => {
    const child = (position) =>
      outputItem(`step ${position}`, { id: `step-${position}`, position });
    const gap = (before) => ({
      id: 'gap',
      type: 'history_gap',
      before: `step-${before}`,
      position: -1,
    });
    const session = { ...state(), historyGeneration: 'g' };
    applyTimelineEvent(
      session,
      'timeline_snapshot',
      snapshot([
        assistantRun('one', [gap(8), child(8), child(9)], { window_start: 8 }),
      ]),
    );
    expect(
      mergeRunChildPage(session, {
        run_id: 'one',
        history_generation: 'g',
        before: 'step-8',
        items: [gap(6), child(6), child(7)],
      }),
    ).toBe(true);
    applyTimelineEvent(
      session,
      'timeline_snapshot',
      snapshot(
        [
          assistantRun('one', [gap(9), child(9), child(10)], {
            window_start: 9,
          }),
        ],
        11,
      ),
    );
    expect(session.timelineItems[0].items.map((item) => item.position)).toEqual(
      [-1, 6, 7, 8, 9, 10],
    );
    applyTimelineEvent(
      session,
      'timeline_update',
      update(
        {
          run: { id: session.timelineItems[0].id },
          windowed: true,
          children: [child(11)],
          positions: { 'step-6': 7, 'step-7': 6 },
        },
        12,
        11,
      ),
    );
    expect(session.timelineItems[0].items.at(-1).position).toBe(11);
    expect(
      session.timelineItems[0].items.slice(1, 3).map((item) => item.id),
    ).toEqual(['step-7', 'step-6']);
    expect(
      mergeRunChildPage(session, {
        run_id: 'one',
        history_generation: 'other',
        items: [child(0)],
      }),
    ).toBe(false);
    loadTimelineItems(
      session,
      [assistantRun('one', [gap(10), child(10)], { window_start: 10 })],
      { reset: true },
    );
    expect(session.timelineItems[0].items.map((item) => item.position)).toEqual(
      [-1, 10],
    );
  });

  it('gives the viewport bounded parts of a single long Run with stable identities', () => {
    const children = Array.from({ length: 1000 }, (_, position) =>
      outputItem(`step ${position}`, { id: `step-${position}`, position }),
    );
    const first = timelineRowsForViewport([assistantRun('one', children)]);
    expect(
      Math.max(...first.map((part) => part.items.length)),
    ).toBeLessThanOrEqual(30);
    expect(first.flatMap((part) => part.items)).toEqual(children);
    expect(first.at(-1).segment_tail).toBe(true);
    expect(
      first.slice(0, -1).every((part) => part.segment_tail === false),
    ).toBe(true);
    const next = timelineRowsForViewport([
      assistantRun('one', [
        ...children,
        outputItem('next', { id: 'next', position: 1000 }),
      ]),
    ]);
    expect(next.slice(0, -1).map((part) => part.id)).toEqual(
      first.slice(0, -1).map((part) => part.id),
    );
  });
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
