import { describe, expect, it } from 'vitest';
import {
  compactToolValue,
  imageReferenceLabel,
  isRowCancellable,
  isRunChildWorking,
  isToolPreparing,
  toolArgumentSummary,
  toolRowFromEvent,
  toolDetailPresentation,
  toolRowPresentation,
  toolStatus,
  toolStatusLabel,
  visibleRunChildren,
} from '../chatTimelinePresentation.js';

import { setupChatTimelinePresentationSuite } from './chatTimelinePresentation.support.js';

describe('chatTimelinePresentation', () => {
  setupChatTimelinePresentationSuite();

  it('uses the durable image reference as the attachment display label', () => {
    expect(
      imageReferenceLabel({
        type: 'media',
        attachment_id: 'image-one',
        filename: 'image.png',
        media_type: 'image/png',
        image_reference: 1,
      }),
    ).toBe('Image 1');
  });

  it('unwraps successful read content and hides envelope metadata', () => {
    const value = compactToolValue(
      {
        ok: true,
        data: { content: 'file contents' },
        artifacts: [{ id: 'internal' }],
      },
      { preferPayload: true, toolName: 'read' },
    );

    expect(value).toBe('file contents');
  });

  it('renders actual line breaks in nested Tool Result strings', () => {
    const value = compactToolValue(
      {
        ok: true,
        data: {
          summary: 'first line\nsecond line',
          nested: { text: 'nested first\r\nnested second' },
          literal: 'keep \\n as text',
        },
      },
      { preferPayload: true, toolName: 'probe' },
    );

    expect(value).toBe(
      'summary: first line\n  second line\n' +
        'nested: text: nested first\r\n    nested second\n' +
        'literal: keep \\n as text',
    );
  });

  it('renders Tool Args line breaks without JSON string wrappers', () => {
    const value = compactToolValue({ query: 'first\nsecond' });

    expect(value).toBe('query: first\n  second');
  });

  it('keeps scalar types available after removing String wrappers', () => {
    const presentation = toolDetailPresentation({
      stringFalse: 'false',
      booleanFalse: false,
      count: 3,
      missing: null,
    });

    expect(presentation.fields).toEqual([
      { key: 'stringFalse', kind: 'string', text: 'false' },
      { key: 'booleanFalse', kind: 'boolean', text: 'false' },
      { key: 'count', kind: 'number', text: '3' },
      { key: 'missing', kind: 'null', text: 'null' },
    ]);
  });

  it('uses the path summary without exposing edit replacement text', () => {
    const summary = toolArgumentSummary({
      name: 'edit',
      arguments: {
        path: 'notes/plan.md',
        old_string: 'before',
        new_string: 'after',
      },
    });

    expect(summary).toBe('notes/plan.md');
  });

  it('labels a still-streaming tool row from its preview arguments', () => {
    const summary = toolArgumentSummary({
      name: 'write',
      arguments: undefined,
      previewArguments: { path: 'notes/plan.md' },
      partialArgumentsText: '{"path": "notes/plan.md", "content": "# Pl',
    });

    expect(summary).toBe('notes/plan.md');
  });

  it('prefers parsed arguments over stale preview arguments', () => {
    const summary = toolArgumentSummary({
      name: 'write',
      arguments: { path: 'final/path.md', content: '# Done' },
      previewArguments: { path: 'stale/path.md' },
    });

    expect(summary).toBe('final/path.md');
  });

  it('summarizes the current flat process action contract', () => {
    const summary = toolArgumentSummary({
      name: 'process',
      arguments: { action: 'status', process_id: 'process-1' },
    });

    expect(summary).toBe('status · process-1');
  });

  it('keeps legacy process request operations visible after history reload', () => {
    const summary = toolArgumentSummary({
      name: 'process',
      arguments: { request: { operation: 'list' } },
    });

    expect(summary).toBe('list');
  });

  it('keeps a streaming row unlabeled while no preview field is complete', () => {
    const summary = toolArgumentSummary({
      name: 'write',
      arguments: undefined,
      previewArguments: null,
      partialArgumentsText: '{"pa',
    });

    expect(summary).toBe('');
  });

  it('keeps structured facts intact while truncating a long description', () => {
    const presentation = toolRowPresentation({
      name: 'grep',
      display: {
        version: 1,
        primary: [
          {
            kind: 'description',
            value:
              'Find every version variable and every derived alias across all runtime packages',
            truncate: 'end',
            tooltip: 'truncated',
            max_characters: 64,
            quote: true,
          },
        ],
        facts: [{ kind: 'count', value: 10, unit: 'matches', at_least: false }],
      },
    });

    expect(presentation.primary[0].text).toHaveLength(64);
    expect(presentation.primary[0].text.endsWith('…')).toBe(true);
    expect(presentation.primary[0].tooltipText).toContain(
      'across all runtime packages',
    );
    expect(presentation.facts[0].text).toBe('10 matches');
  });

  it('compacts a deep read path from the start and keeps its full tooltip', () => {
    const presentation = toolRowPresentation({
      name: 'read',
      display: {
        version: 1,
        primary: [
          {
            kind: 'path',
            value: 'C:/workspace/packages/chat/components/ToolRow.svelte',
            full_value: 'C:/workspace/packages/chat/components/ToolRow.svelte',
            truncate: 'start',
            tooltip: 'always',
            max_characters: 64,
            copyable: true,
          },
        ],
        facts: [],
      },
    });

    expect(presentation.primary[0]).toMatchObject({
      text: '…/chat/components/ToolRow.svelte',
      fullText: 'C:/workspace/packages/chat/components/ToolRow.svelte',
      copyable: true,
    });
  });

  it('preserves filename-only read labels', () => {
    const presentation = toolRowPresentation({
      name: 'read',
      display: {
        primary: [
          {
            kind: 'path',
            value: 'ToolRow.svelte',
            full_value: 'C:/workspace/ToolRow.svelte',
            truncate: 'start',
            tooltip: 'always',
          },
        ],
      },
    });

    expect(presentation.primary[0].text).toBe('ToolRow.svelte');
    expect(presentation.primary[0].tooltipText).toBe(
      'C:/workspace/ToolRow.svelte',
    );
  });

  it('middle-truncates URLs and localizes singular and lower-bound counts', () => {
    const presentation = toolRowPresentation({
      name: 'web_search',
      display: {
        primary: [
          {
            kind: 'url',
            value:
              'https://example.com/a/very/long/path/to/a/result?query=versions',
            truncate: 'middle',
            max_characters: 24,
          },
        ],
        facts: [
          { kind: 'count', value: 1, unit: 'results', at_least: false },
          { kind: 'count', value: 10, unit: 'matches', at_least: true },
        ],
      },
    });

    expect(presentation.primary[0].text).toHaveLength(24);
    expect(presentation.primary[0].text).toContain('…');
    expect(presentation.facts.map((fact) => fact.text)).toEqual([
      '1 result',
      '10+ matches',
    ]);
  });

  it('renders multi-edit counts with Agent-safe labels', () => {
    const presentation = toolRowPresentation({
      name: 'edit',
      display: {
        facts: [
          { kind: 'count', value: 3, unit: 'edits', at_least: false },
          { kind: 'count', value: 2, unit: 'files', at_least: false },
          { kind: 'count', value: 1, unit: 'failures', at_least: false },
        ],
      },
    });

    expect(presentation.facts.map((fact) => fact.text)).toEqual([
      '3 edits',
      '2 files',
      '1 failed',
    ]);
  });

  it('recognizes partial Tool results and labels them independently of failure', () => {
    const tool = toolRowFromEvent({
      type: 'tool_call_result',
      payload: {
        tool_call: { id: 'edit-1', name: 'edit' },
        result: {
          ok: true,
          error: null,
          data: { status: 'partial', succeeded: 2, failed: 1 },
          artifacts: [],
        },
        timing: {
          started_at: '2026-08-28T00:00:00.000Z',
          completed_at: '2026-08-28T00:00:00.240Z',
        },
      },
    });

    expect(toolStatus(tool)).toBe('partial');
    expect(toolStatusLabel(tool)).toBe('partial · 0.2s');
  });

  it('does not expose arbitrary arguments for an unknown Tool', () => {
    const presentation = toolRowPresentation({
      name: 'extension_private_probe',
      arguments: { token: 'do-not-render', operation: 'inspect' },
    });

    expect(presentation).toEqual({ primary: [], facts: [] });
  });

  it('marks only running bash tool rows as cancellable', () => {
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'running',
      }),
    ).toBe(true);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'success',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'failed',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'cancelled',
      }),
    ).toBe(false);
  });

  it('does not mark streaming preview tool rows as cancellable', () => {
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'bash',
        toolStatus: 'running',
        streaming: true,
      }),
    ).toBe(false);
  });

  it('renders a streaming preview tool row before its started event', () => {
    const assistantRun = {
      items: [
        {
          type: 'tool_call',
          streaming: true,
          name: 'session_search',
          partialArgumentsText: '{"query": "ca',
          startedEvent: null,
          resultEvent: null,
          stdout: '',
          stderr: '',
        },
      ],
    };

    expect(visibleRunChildren(assistantRun)).toHaveLength(1);
  });

  it('marks only the latest visible streaming child as the active work', () => {
    const reasoning = {
      id: 'reasoning-one',
      type: 'reasoning',
      content: 'Inspect the request.',
      streaming: true,
    };
    const answer = {
      id: 'answer-one',
      type: 'assistant_output',
      content: 'I will inspect it.',
      streaming: true,
    };
    const tool = {
      id: 'tool-one',
      type: 'tool_call',
      name: 'read',
      streaming: true,
      status: 'preparing',
    };
    const assistantRun = {
      status: 'running',
      items: [reasoning, answer, tool],
    };

    expect(isRunChildWorking(assistantRun, reasoning)).toBe(false);
    expect(isRunChildWorking(assistantRun, answer)).toBe(false);
    expect(isRunChildWorking(assistantRun, tool)).toBe(true);
  });

  it('does not expose working text after the Run becomes terminal', () => {
    const answer = {
      id: 'answer-one',
      type: 'assistant_output',
      content: 'Done.',
      streaming: true,
    };

    expect(
      isRunChildWorking({ status: 'completed', items: [answer] }, answer),
    ).toBe(false);
  });

  it('does not mark non-bash tool rows as cancellable', () => {
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'read',
        toolStatus: 'running',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'edit',
        toolStatus: 'running',
      }),
    ).toBe(false);
    expect(
      isRowCancellable({
        kind: 'tool_call',
        toolName: 'grep',
        toolStatus: 'running',
      }),
    ).toBe(false);
  });

  it('marks only running sub-agent rows as cancellable', () => {
    expect(isRowCancellable({ kind: 'sub_agent', dotStatus: 'running' })).toBe(
      true,
    );
    expect(isRowCancellable({ kind: 'sub_agent', dotStatus: 'success' })).toBe(
      false,
    );
    expect(isRowCancellable({ kind: 'sub_agent', dotStatus: 'failed' })).toBe(
      false,
    );
    expect(
      isRowCancellable({ kind: 'sub_agent', dotStatus: 'cancelled' }),
    ).toBe(false);
  });

  it('rejects unknown row shapes', () => {
    expect(isRowCancellable(null)).toBe(false);
    expect(isRowCancellable(undefined)).toBe(false);
    expect(isRowCancellable({})).toBe(false);
    expect(isRowCancellable({ kind: 'reasoning' })).toBe(false);
  });
});

describe('isToolPreparing', () => {
  it('is true only while a tool call is still streaming (not yet dispatched)', () => {
    expect(isToolPreparing({ status: 'preparing' })).toBe(true);
  });

  it('is false once the call is dispatched or settled', () => {
    for (const status of [
      'running',
      'success',
      'completed',
      'failed',
      'cancelled',
    ]) {
      expect(isToolPreparing({ status })).toBe(false);
    }
  });

  it('tolerates a missing tool', () => {
    expect(isToolPreparing(null)).toBe(false);
    expect(isToolPreparing(undefined)).toBe(false);
  });
});
