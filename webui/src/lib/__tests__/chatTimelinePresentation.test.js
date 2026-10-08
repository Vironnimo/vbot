import { describe, expect, it } from 'vitest';
import {
  compactionSeparatorDetails,
  compactionSeparatorLabel,
  compactionSummaryText,
  fileMentionDetails,
  errorMessagePresentation,
  groupTransientCards,
  imageReferenceLabel,
  isToolPreparing,
  labelForEvent,
  labelForMessage,
  takeoverSeparatorLabel,
  toolDetailPresentation,
  toolRowFromEvent,
  toolRowPresentation,
  toolStatus,
  toolStatusDetails,
  toolStatusLabel,
} from '../chatTimelinePresentation.js';
import { t } from '../i18n.js';
import { formatMoment } from '../timeText.js';

// The one-line summary a Tool row shows next to its name.
function primaryText(tool) {
  return toolRowPresentation(tool)
    .primary.map((part) => part.text)
    .join(' · ');
}

describe('message presentation', () => {
  const via = (name) => t('chat.errorViaProvider', { name });

  it.each([
    [
      'a nested Provider message',
      'Provider error: 400 {"error":{"message":"max_tokens: Field required","code":"invalid_request_body"}}',
      'Provider error: 400 max_tokens: Field required',
      '"code": "invalid_request_body"',
    ],
    [
      'a top-level message field',
      'Provider error: 400 {"message":"max_tokens: Field required"}',
      'Provider error: 400 max_tokens: Field required',
      '"message"',
    ],
    [
      'the deepest error.message over sibling fields',
      'Rate limited: 429 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"},"request_id":"req_1"}',
      'Rate limited: 429 Overloaded',
      '"request_id": "req_1"',
    ],
    [
      'the prefix when the body has no message',
      'Provider error: 500 {"status":"boom"}',
      'Provider error: 500',
      '"status": "boom"',
    ],
    [
      'the upstream rate-limit detail and router Provider name',
      'Rate limited: 429 {"error":{"message":"Provider returned error","code":429,' +
        '"metadata":{"raw":"stealth/ox-alpha is temporarily rate-limited upstream. Please retry shortly.",' +
        '"provider_name":"Stealth","remedy_hint":"Retry shortly, add your own provider key"}},"user_id":"u1"}',
      `Rate limited: 429 stealth/ox-alpha is temporarily rate-limited upstream. Please retry shortly. ${via('Stealth')}`,
      '"remedy_hint"',
    ],
    [
      'router metadata from a top-level error body',
      'Provider returned error: {"message":"Provider returned error","code":502,' +
        '"metadata":{"raw":"upstream connection reset","provider_name":"Morph"}}',
      `Provider returned error: upstream connection reset ${via('Morph')}`,
      '"provider_name": "Morph"',
    ],
    [
      'an upstream detail equal to the message only once',
      'Provider error: 500 {"message":"overloaded","metadata":{"raw":"overloaded"}}',
      'Provider error: 500 overloaded',
      '"raw": "overloaded"',
    ],
    [
      'an in-band message its prefix repeats only once',
      'Test failure. Please retry.: {"type":"error","status":503,' +
        '"error":{"message":"Test failure. Please retry.","code":"test_code"}}',
      'Test failure. Please retry.',
      '"status": 503',
    ],
    [
      'a repeated in-band message once with its router Provider name',
      'Provider returned error: {"message":"Provider returned error","code":502,' +
        '"metadata":{"provider_name":"Morph"}}',
      `Provider returned error ${via('Morph')}`,
      '"code": 502',
    ],
    [
      'the prefix without its separator when the body has no message',
      'Responses request failed: {"code":"test_code"}',
      'Responses request failed',
      '"code": "test_code"',
    ],
    ['plain text unchanged', 'Connection refused', 'Connection refused', ''],
    [
      'the full text when the embedded JSON does not parse',
      'Provider error: 400 {broken json',
      'Provider error: 400 {broken json',
      '',
    ],
    ['nothing for non-string input', null, '', ''],
  ])('summarizes an error with %s', (_label, text, summary, details) => {
    const presentation = errorMessagePresentation(text);

    expect(presentation.summary).toBe(summary);
    if (details) {
      expect(presentation.details).toContain(details);
    } else {
      expect(presentation.details).toBe('');
    }
  });

  it.each([
    [
      'the sender display name',
      { id: '50', display_name: 'Alice' },
      {},
      'ALICE',
    ],
    ['no sender', undefined, {}, ''],
    ['a blank sender name', { id: '50', display_name: '   ' }, {}, ''],
    [
      'the Live voice origin',
      undefined,
      { input_origin: 'live_voice' },
      t('chat.role.userViaLive', { name: t('chat.role.user') }).toUpperCase(),
    ],
    ['a dictation', undefined, { input_origin: 'speech_transcription' }, ''],
  ])(
    'labels persisted and live User messages with %s',
    (_label, sender, extra, expected) => {
      const message = { role: 'user', content: 'hello', sender, ...extra };

      expect(labelForMessage(message)).toBe(expected);
      expect(
        labelForEvent({ type: 'user_message_persisted', payload: { message } }),
      ).toBe(expected);
    },
  );

  it('labels an image attachment by its durable image reference', () => {
    expect(
      imageReferenceLabel({
        type: 'media',
        attachment_id: 'image-one',
        filename: 'image.png',
        media_type: 'image/png',
        image_reference: 1,
      }),
    ).toBe(t('chat.attachment.imageReference', { number: 1 }));
  });

  it('returns the checkpoint summary byte-for-byte', () => {
    const summary = '\n# Heading\n\n<tag> & *literal*\n';

    expect(
      compactionSummaryText({
        message: { role: 'compaction_checkpoint', content: summary },
      }),
    ).toBe(summary);
    expect(compactionSummaryText({ message: { content: null } })).toBe('');
    expect(compactionSummaryText(null)).toBe('');
  });

  it.each([
    [
      'duration and abbreviated tokens',
      {
        durationMs: 45_000,
        contextTokensBefore: 254_224,
        contextTokensAfter: 40_289,
      },
      () =>
        t('chat.compactedWithTimingTokens', {
          duration: t('chat.durationSeconds', { seconds: 45 }),
          before: '254k',
          after: '40k',
        }),
    ],
    [
      'a minute-scale duration',
      { durationMs: 85_000 },
      () =>
        t('chat.compactedWithTiming', {
          duration: t('chat.durationMinutesSeconds', {
            minutes: 1,
            seconds: 25,
          }),
        }),
    ],
    [
      'tokens without a known duration (reloaded History)',
      { contextTokensBefore: 254_224, contextTokensAfter: 40_289 },
      () => t('chat.compactedWithTokens', { before: '254k', after: '40k' }),
    ],
    ['neither tokens nor duration', {}, () => t('chat.compacted')],
  ])('labels a completed Compaction with %s', (_label, fields, expected) => {
    expect(
      compactionSeparatorLabel({
        status: 'completed',
        message: { role: 'compaction_checkpoint', content: 'summary' },
        ...fields,
      }),
    ).toBe(expected());
  });

  it.each([
    ['running', 'chat.compactingCurrentConversation'],
    ['failed', 'chat.compactionFailed'],
  ])('labels a %s Compaction', (status, key) => {
    expect(compactionSeparatorLabel({ status, message: null })).toBe(t(key));
    expect(compactionSeparatorDetails({ status, message: null })).toBeNull();
  });

  it('details a completed Compaction with exact sizes, savings and moment', () => {
    const timestamp = '2026-09-29T10:00:00Z';
    const nowMs = Date.parse(timestamp) + 120_000;

    expect(
      compactionSeparatorDetails(
        {
          type: 'compaction_separator',
          status: 'completed',
          timestamp,
          contextTokensBefore: 200_000,
          contextTokensAfter: 50_000,
        },
        nowMs,
      ),
    ).toEqual({
      rows: [
        {
          label: t('chat.compaction.before'),
          value: t('chat.compaction.tokens', { count: '200,000' }),
        },
        {
          label: t('chat.compaction.after'),
          value: t('chat.compaction.tokens', { count: '50,000' }),
        },
        { label: t('chat.compaction.saved'), value: '75%' },
        {
          label: t('chat.compaction.when'),
          value: formatMoment(timestamp, { nowMs }),
        },
      ],
    });
  });

  it.each([
    [
      'an included file with its size',
      { path: 'src/app.js', status: 'inlined', size_bytes: 12_400 },
      () => t('chat.fileMention.inlinedSize', { size: '12.4 kB' }),
    ],
    [
      'a file referenced only by path',
      { path: 'src/app.js', status: 'too_large', size_bytes: 9_000_000 },
      () => null,
    ],
  ])('details %s', (_label, block, content) => {
    const rows = [
      { label: t('chat.fileMention.path'), value: 'src/app.js', mono: true },
    ];
    if (content()) {
      rows.push({ label: t('chat.fileMention.content'), value: content() });
    }

    expect(fileMentionDetails({ type: 'file_mention', ...block })).toEqual({
      title: t('chat.fileMention.label'),
      rows,
    });
  });

  it('labels an Agent takeover with its raw addresses or a generic fallback', () => {
    expect(
      takeoverSeparatorLabel({
        content: JSON.stringify({ from: 'reviewer@vbot', to: 'assistant' }),
      }),
    ).toBe(t('chat.takenOver', { from: 'reviewer@vbot', to: 'assistant' }));

    const generic = t('chat.takenOverGeneric');
    for (const message of [
      { content: 'not json' },
      { content: '' },
      { content: JSON.stringify({ from: 'a' }) },
      { content: JSON.stringify({ to: 'b' }) },
      {},
      null,
    ]) {
      expect(takeoverSeparatorLabel(message)).toBe(generic);
    }
  });
});

describe('groupTransientCards', () => {
  const runItem = (id, timestamp) => ({
    id,
    type: 'assistant_run',
    timestamp,
    items: [],
  });
  const userItem = (id, timestamp) => ({
    id,
    type: 'message',
    message: { id, role: 'user', timestamp },
  });
  const card = (anchorId, createdAt) => ({
    id: 'card-1',
    text: 'Agent: Alpha',
    anchorId,
    ...(createdAt === undefined ? {} : { createdAt }),
  });

  it('anchors a card after the exact item it followed at creation', () => {
    const groups = groupTransientCards(
      [
        userItem('user-1', '2026-08-27T13:40:00Z'),
        runItem('run-live', '2026-08-27T13:46:00Z'),
      ],
      [card('run-live', 0)],
    );

    expect(groups.byItemId.get('run-live')).toHaveLength(1);
    expect(groups.leading).toEqual([]);
    expect(groups.trailing).toEqual([]);
  });

  it('keeps a card whose anchor was replaced at its chronological position', () => {
    // The card was created mid-Run; a History reload replaced the live Run id
    // and a newer message arrived. The card sits between them.
    const groups = groupTransientCards(
      [
        userItem('user-1', '2026-08-27T13:40:00Z'),
        runItem('run-history', '2026-08-27T13:46:00Z'),
        userItem('user-2', '2026-08-27T13:48:00Z'),
      ],
      [card('run-live', Date.parse('2026-08-27T13:46:30Z'))],
    );

    expect(groups.byItemId.size).toBe(0);
    expect(groups.byItemIndex.get(1)).toHaveLength(1);
    expect(groups.trailing).toEqual([]);
  });

  it('falls back to the timeline end or start without a usable anchor', () => {
    const items = [
      userItem('user-1', '2026-08-27T13:40:00Z'),
      runItem('run-history', '2026-08-27T13:46:00Z'),
    ];

    expect(
      groupTransientCards(items, [card('run-live')]).trailing,
    ).toHaveLength(1);
    // A card created on an empty timeline stays at the top.
    expect(
      groupTransientCards(items, [card(null, Date.now())]).leading,
    ).toHaveLength(1);
  });
});

describe('Tool row presentation', () => {
  it.each([
    [
      'ripgrep arguments',
      'search_files',
      { args: ['-F', 'call(', 'src'] },
      '-F call( src',
    ],
    [
      'named search fields before extra arguments',
      'search_files',
      { pattern: 'load', path: ['src', 'docs'], glob: '*.py', args: ['-i'] },
      'load · src, docs · *.py · -i',
    ],
    ['a lone glob', 'search_files', { glob: '*.md' }, '*.md'],
    [
      'search pattern arrays',
      'search_files',
      { action: 'content', patterns: ['alpha', 'beta'] },
      'alpha, beta',
    ],
    [
      'the edit path without replacement text',
      'edit',
      { path: 'notes/plan.md', old_string: 'before', new_string: 'after' },
      'notes/plan.md',
    ],
    [
      'a cron job by id',
      'cron',
      { action: 'disable', id: 'cron_abc' },
      'disable · cron_abc',
    ],
  ])('summarizes %s', (_label, name, args, summary) => {
    expect(primaryText({ name, arguments: args })).toBe(summary);
  });

  it.each([
    [
      'from its preview arguments',
      {
        name: 'write',
        previewArguments: { path: 'notes/plan.md' },
        partialArgumentsText: '{"path": "notes/plan.md", "content": "# Pl',
      },
      'notes/plan.md',
    ],
    [
      'from the cron action and schedule',
      {
        name: 'cron',
        previewArguments: {
          action: 'create',
          prompt: 'Summarize the queue.',
          schedule: 'every 2h',
        },
      },
      'create · every 2h',
    ],
    [
      'with parsed arguments over stale preview arguments',
      {
        name: 'write',
        arguments: { path: 'final/path.md', content: '# Done' },
        previewArguments: { path: 'stale/path.md' },
      },
      'final/path.md',
    ],
    [
      'empty while no preview field is complete',
      { name: 'write', previewArguments: null, partialArgumentsText: '{"pa' },
      '',
    ],
  ])('labels a streaming Tool row %s', (_label, tool, summary) => {
    expect(primaryText(tool)).toBe(summary);
  });

  it('does not expose arbitrary arguments of an unknown Tool', () => {
    expect(
      toolRowPresentation({
        name: 'extension_private_probe',
        arguments: { token: 'do-not-render', operation: 'inspect' },
      }),
    ).toEqual({ primary: [], facts: [] });
  });

  it('truncates a long description while keeping structured facts intact', () => {
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
    expect(presentation.primary[0].reveal).toMatchObject({
      value:
        'Find every version variable and every derived alias across all runtime packages',
      mono: false,
      whenTruncated: false,
      copy: null,
    });
    expect(presentation.facts[0].text).toBe(
      t('chat.toolFact.matches', { count: '10' }),
    );
  });

  it('compacts a deep path from the start and keeps short paths whole', () => {
    const [deep] = toolRowPresentation({
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
    }).primary;
    const [short] = toolRowPresentation({
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
    }).primary;

    expect(deep).toMatchObject({
      text: '…/chat/components/ToolRow.svelte',
      reveal: {
        value: 'C:/workspace/packages/chat/components/ToolRow.svelte',
        mono: true,
        copy: {
          label: t('chat.copyPath'),
          copiedLabel: t('chat.pathCopied'),
        },
      },
    });
    expect(short.text).toBe('ToolRow.svelte');
    expect(short.reveal).toMatchObject({
      value: 'C:/workspace/ToolRow.svelte',
      whenTruncated: false,
      copy: null,
    });
  });

  it('reveals the command a Bash description stands for and fitting values only while clipped', () => {
    const [described] = toolRowPresentation({
      name: 'bash',
      display: {
        primary: [
          {
            kind: 'description',
            value: 'Run the test suite',
            truncate: 'end',
            tooltip: 'always',
            quote: true,
            copyable: true,
            detail: 'python -m pytest\n  -x',
            detail_kind: 'command',
          },
        ],
      },
    }).primary;
    const [command, query] = toolRowPresentation({
      name: 'bash',
      display: {
        primary: [
          { kind: 'command', value: 'git status', copyable: true },
          { kind: 'query', value: 'svelte tooltips', quote: true },
        ],
      },
    }).primary;
    const [silent] = toolRowPresentation({
      name: 'terminal',
      display: {
        primary: [{ kind: 'identifier', value: 'term_one', tooltip: 'none' }],
      },
    }).primary;

    expect(described).toMatchObject({
      text: 'Run the test suite',
      reveal: {
        title: 'Run the test suite',
        value: 'python -m pytest\n  -x',
        mono: true,
        whenTruncated: false,
        copy: {
          label: t('chat.copyCommand'),
          copiedLabel: t('chat.commandCopied'),
        },
      },
    });
    expect(command.reveal).toMatchObject({
      title: '',
      value: 'git status',
      mono: true,
      whenTruncated: true,
    });
    expect(query.reveal).toMatchObject({ mono: false, copy: null });
    expect(silent.reveal).toBeNull();
  });

  it('middle-truncates URLs and localizes singular, lower-bound and failure counts', () => {
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
          { kind: 'count', value: 3, unit: 'edits', at_least: false },
          { kind: 'count', value: 2, unit: 'files', at_least: false },
          { kind: 'count', value: 1, unit: 'failures', at_least: false },
        ],
      },
    });

    expect(presentation.primary[0].text).toHaveLength(24);
    expect(presentation.primary[0].text).toContain('…');
    expect(presentation.facts.map((fact) => fact.text)).toEqual([
      t('chat.toolFact.result', { count: '1' }),
      t('chat.toolFact.matches', { count: '10+' }),
      t('chat.toolFact.edits', { count: '3' }),
      t('chat.toolFact.files', { count: '2' }),
      t('chat.toolFact.failure', { count: '1' }),
    ]);
  });

  it('labels a partial Tool result independently of failure', () => {
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
    expect(toolStatusLabel(tool)).toBe(
      [
        t('chat.toolPartial'),
        t('chat.durationSeconds', { seconds: '0.2' }),
      ].join(' · '),
    );
  });

  it('details the Tool call state, moments and runtime behind its status', () => {
    const startedAt = '2026-08-28T00:00:00.000Z';
    const completedAt = '2026-08-28T00:00:00.240Z';
    const nowMs = Date.parse(completedAt) + 1000;
    const moment = (value) => formatMoment(value, { nowMs, seconds: true });
    const partial = toolRowFromEvent({
      type: 'tool_call_result',
      payload: {
        tool_call: { id: 'edit-1', name: 'edit' },
        result: { ok: true, error: null, data: { status: 'partial' } },
        timing: { started_at: startedAt, completed_at: completedAt },
      },
    });

    // It finished within the second it started, so no Finished row repeats it.
    expect(toolStatusDetails(partial, nowMs)).toEqual({
      title: t('chat.toolState.partial'),
      text: t('chat.toolState.partialHint'),
      rows: [
        { label: t('chat.details.started'), value: moment(startedAt) },
        {
          label: t('chat.details.duration'),
          value: t('chat.durationSeconds', { seconds: '0.2' }),
        },
      ],
    });
    expect(toolStatusDetails({ status: 'preparing' }, nowMs)).toEqual({
      title: t('chat.toolState.preparing'),
      text: t('chat.toolState.preparingHint'),
    });
  });

  it('reads a Tool call its ended Run never answered as interrupted, not running', () => {
    const startedAt = '2026-08-28T00:00:00.000Z';
    const nowMs = Date.parse(startedAt) + 60_000;
    const tool = {
      name: 'bash',
      status: 'interrupted',
      startedEvent: { timestamp: startedAt },
    };

    expect(toolStatus(tool)).toBe('interrupted');
    // No ticking runtime: the call has no end to measure against.
    expect(toolStatusLabel(tool, nowMs)).toBe(t('chat.toolInterrupted'));
    expect(toolStatusDetails(tool, nowMs)).toMatchObject({
      title: t('chat.toolState.interrupted'),
      text: t('chat.toolState.interruptedHint'),
    });
  });

  it.each([
    ['preparing', true],
    ['running', false],
    ['success', false],
    ['failed', false],
    ['cancelled', false],
  ])('treats a %s Tool call as preparing: %s', (status, preparing) => {
    expect(isToolPreparing({ status })).toBe(preparing);
  });

  it('tolerates a missing Tool when checking preparation', () => {
    expect(isToolPreparing(null)).toBe(false);
    expect(isToolPreparing(undefined)).toBe(false);
  });
});

describe('Tool detail values', () => {
  it('unwraps successful read content and hides envelope metadata', () => {
    expect(
      toolDetailPresentation(
        {
          ok: true,
          data: { content: 'file contents' },
          artifacts: [{ id: 'internal' }],
        },
        { preferPayload: true, toolName: 'read' },
      ).copyText,
    ).toBe('file contents');
  });

  it('keeps search continuation metadata and warnings', () => {
    const value = toolDetailPresentation(
      {
        ok: true,
        data: {
          content: 'src/a.py:1:alpha',
          next_offset: 1,
          complete: false,
          warnings: ['Search timed out'],
        },
      },
      { preferPayload: true, toolName: 'search_files' },
    ).copyText;

    expect(value).toContain('next_offset');
    expect(value).toContain('Search timed out');
  });

  it('renders real line breaks in result and argument strings but keeps escaped ones', () => {
    expect(
      toolDetailPresentation(
        {
          ok: true,
          data: {
            summary: 'first line\nsecond line',
            nested: { text: 'nested first\r\nnested second' },
            literal: 'keep \\n as text',
          },
        },
        { preferPayload: true, toolName: 'probe' },
      ).copyText,
    ).toBe(
      'summary: first line\n  second line\n' +
        'nested: text: nested first\r\n    nested second\n' +
        'literal: keep \\n as text',
    );
    expect(toolDetailPresentation({ query: 'first\nsecond' }).copyText).toBe(
      'query: first\n  second',
    );
  });

  it('keeps scalar types after removing string wrappers', () => {
    expect(
      toolDetailPresentation({
        stringFalse: 'false',
        booleanFalse: false,
        count: 3,
        missing: null,
      }).fields,
    ).toEqual([
      { key: 'stringFalse', kind: 'string', text: 'false' },
      { key: 'booleanFalse', kind: 'boolean', text: 'false' },
      { key: 'count', kind: 'number', text: '3' },
      { key: 'missing', kind: 'null', text: 'null' },
    ]);
  });
});
