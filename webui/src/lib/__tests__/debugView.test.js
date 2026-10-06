import { describe, expect, it } from 'vitest';

import { setApplicationTimeZone } from '../dateTimePrefs.svelte.js';

import {
  applyDebugStatus,
  applyModelProbeProviders,
  applyModelProbeResult,
  applyTraceDetail,
  applyTraceList,
  bodyMatchOffsets,
  clearTracesApplied,
  createDebugViewState,
  filterTraces,
  formatHeadersForDisplay,
  formatTraceStatus,
  formattedBodyText,
  hasParseableBody,
  modelProbeCanProbe,
  modelProbeConnectionOptions,
  rawBodyText,
  selectModelProbeConnection,
  selectModelProbeProvider,
  selectTrace,
  traceLabel,
  traceStatusTone,
  traceTooltip,
  traceTypeLabel,
} from '../debugView.js';

describe('debugView trace state', () => {
  it('creates independent empty states', () => {
    const first = createDebugViewState();
    expect(first).toEqual({
      traces: [],
      selectedTrace: null,
      loading: false,
      error: '',
      modelProbeProviders: [],
      modelProbeProvider: '',
      modelProbeConnection: '',
      modelProbeResult: null,
      modelProbeLoading: false,
      modelProbeError: '',
    });
    first.traces.push(traceEntry('a'));
    expect(createDebugViewState().traces).toEqual([]);
  });

  it('normalizes the trace list, keeping unknown status and duration null', () => {
    const state = createDebugViewState();
    state.loading = true;
    state.error = 'stale';

    const traces = applyTraceList(state, {
      traces: [
        traceEntry('t-1'),
        null,
        {},
        { trace_id: '  ' },
        { trace_id: 't-2', status_code: 'oops', duration_ms: 12.5 },
        { trace_id: 't-3', status_code: null, duration_ms: null },
      ],
    });

    expect(traces).toBe(state.traces);
    expect(traces).toEqual([
      traceEntry('t-1'),
      {
        trace_id: 't-2',
        timestamp: '',
        provider_id: '',
        model_id: '',
        method: '',
        url: '',
        status_code: null,
        duration_ms: null,
        type: null,
      },
      expect.objectContaining({
        trace_id: 't-3',
        status_code: null,
        duration_ms: null,
      }),
    ]);
    expect(state.loading).toBe(false);
    expect(state.error).toBe('');

    expect(applyTraceList(state, { traces: 'nope' })).toEqual([]);
  });

  it('keeps the complete selected payload across list refreshes only while its id is listed', () => {
    const state = createDebugViewState();
    applyTraceList(state, { traces: [traceEntry('a'), traceEntry('b')] });
    expect(state.selectedTrace).toBeNull();

    const detail = applyTraceDetail(state, {
      trace: { trace_id: 'a', request: { body: 'verbatim' } },
    });
    applyTraceList(state, {
      traces: [traceEntry('a'), traceEntry('b'), traceEntry('c')],
    });
    expect(state.selectedTrace).toBe(detail);

    applyTraceList(state, { traces: [traceEntry('c')] });
    expect(state.selectedTrace).toBeNull();
  });

  it('selects a listed trace by id and clears the selection for unknown or blank ids', () => {
    const state = createDebugViewState();
    applyTraceList(state, { traces: [traceEntry('t-1'), traceEntry('t-2')] });

    const selected = selectTrace(state, 't-2');
    expect(selected).toMatchObject({ trace_id: 't-2' });
    expect(state.selectedTrace).toBe(selected);

    for (const id of ['missing', '', '   ', null]) {
      selectTrace(state, 't-1');
      expect(selectTrace(state, id)).toBeNull();
      expect(state.selectedTrace).toBeNull();
    }
  });

  it('applies a trace detail as the selection and clears transient flags', () => {
    const state = createDebugViewState();
    state.loading = true;
    state.error = 'stale';
    const trace = {
      trace_id: 't-1',
      request: { method: 'POST', body: '{"x":1}' },
      response: { status_code: 200, body: '{"y":2}' },
    };

    expect(applyTraceDetail(state, { trace })).toBe(trace);
    expect(state.selectedTrace).toBe(trace);
    expect(state.loading).toBe(false);
    expect(state.error).toBe('');

    expect(applyTraceDetail(state, { trace: null })).toBeNull();
    expect(state.selectedTrace).toBeNull();
  });

  it('empties both the list and the selection when traces are cleared', () => {
    const state = createDebugViewState();
    applyTraceList(state, { traces: [traceEntry('t-1')] });
    selectTrace(state, 't-1');

    expect(clearTracesApplied(state)).toEqual([]);
    expect(state.traces).toEqual([]);
    expect(state.selectedTrace).toBeNull();
  });

  it('normalizes the debug status with safe defaults', () => {
    const state = createDebugViewState();
    state.loading = true;
    state.error = 'stale';

    expect(
      applyDebugStatus(state, {
        enabled: true,
        trace_limit: 100,
        trace_count: 12,
        data_directory: 'C:/data',
      }),
    ).toEqual({
      enabled: true,
      traceLimit: 100,
      traceCount: 12,
      dataDirectory: 'C:/data',
    });
    expect(state.error).toBe('');
    expect(state.loading).toBe(false);

    expect(
      applyDebugStatus(state, {
        enabled: 'yes',
        trace_limit: -3,
        trace_count: 4.5,
        data_directory: null,
      }),
    ).toEqual({
      enabled: false,
      traceLimit: 50,
      traceCount: 0,
      dataDirectory: '',
    });
  });
});

describe('debugView trace list projections', () => {
  it('classifies status tones, leaving a missing status unknown', () => {
    expect(
      [null, undefined, 101, 200, 204, 302, 429, 500].map(traceStatusTone),
    ).toEqual([
      'unknown',
      'unknown',
      'ok',
      'ok',
      'ok',
      'unknown',
      'error',
      'error',
    ]);
  });

  it('searches metadata case-insensitively and intersects status and Provider filters', () => {
    const traces = [
      {
        trace_id: 'a',
        provider_id: 'openai',
        model_id: 'gpt',
        status_code: 200,
        url: '/responses',
      },
      {
        trace_id: 'b',
        provider_id: 'openai',
        model_id: 'gpt',
        status_code: 429,
        url: '/responses',
      },
      {
        trace_id: 'c',
        provider_id: 'other',
        model_id: 'gpt',
        status_code: null,
      },
    ];
    expect(filterTraces(traces, 'GPT /RESPONSES', 'error', 'openai')).toEqual([
      traces[1],
    ]);
    expect(filterTraces(traces, '', 'unknown')).toEqual([traces[2]]);
    expect(filterTraces(traces)).toEqual(traces);
  });
  it('names traces by Model, Model Probe or request path', () => {
    expect(
      traceLabel({ model_id: 'openai/gpt-5', type: 'provider_request' }),
    ).toEqual({ text: 'openai/gpt-5', mono: false });
    expect(traceLabel({ model_id: '', type: 'model_probe' })).toEqual({
      text: 'Model Probe',
      mono: false,
    });
    // A detail trace keeps its URL under `request`.
    expect(
      traceLabel({
        type: 'provider_request',
        request: { url: 'https://api.example.com/v1/embeddings?key=x' },
      }),
    ).toEqual({ text: '/v1/embeddings', mono: true });
    expect(traceLabel({ type: 'provider_request', url: 'not a url' })).toEqual({
      text: 'Provider request',
      mono: false,
    });
    expect(traceTypeLabel('future_type')).toBe('future_type');
    expect(formatTraceStatus(429)).toBe('429 Too Many Requests');
    expect(formatTraceStatus(418)).toBe('418');
    expect(formatTraceStatus(null)).toBe('');
  });

  it('builds one trace details card from list and detail shapes alike', () => {
    setApplicationTimeZone('UTC');
    const now = Date.parse('2026-09-29T15:16:00Z');
    const card = traceTooltip(
      {
        trace_id: 'tr_1',
        type: 'provider_request',
        timestamp: '2026-09-29T15:04:05Z',
        provider_id: 'openai',
        model_id: 'openai/gpt-5',
        method: 'POST',
        url: 'https://api.openai.com/v1/responses',
        status_code: 429,
        duration_ms: 1234,
      },
      now,
    );

    expect(card.title).toBe('openai/gpt-5');
    expect(card.text).toBe('Provider request');
    expect(card.placement).toBe('right');
    const rows = Object.fromEntries(card.rows.map((row) => [row.label, row]));
    expect(rows.Request).toMatchObject({
      value: 'POST https://api.openai.com/v1/responses',
      mono: true,
    });
    expect(rows.Status).toMatchObject({
      value: '429 Too Many Requests',
      tone: 'danger',
    });
    expect(rows.Started.value).toMatch(/3:04:05 PM · 12 minutes ago$/);
    expect(rows.Duration.value).toBe('1,234 ms');
    expect(rows.Type).toMatchObject({ value: 'provider_request', mono: true });
    expect(rows['Trace ID'].value).toBe('tr_1');

    const probe = traceTooltip({
      trace_id: 'tr_2',
      type: 'model_probe',
      request: { method: 'GET', url: 'https://api.example.com/v1/models' },
      response: { status_code: 200 },
    });
    // The title already names the type, so no lead line repeats it.
    expect(probe).toMatchObject({ title: 'Model Probe', text: '' });
    expect(probe.rows.find((row) => row.label === 'Status')).toMatchObject({
      value: '200 OK',
      tone: 'success',
    });
  });
});

describe('debugView body projections', () => {
  it('keeps raw bodies verbatim and stringifies only non-string values', () => {
    const cyclic = {};
    cyclic.self = cyclic;

    expect(rawBodyText('{"a":1}')).toBe('{"a":1}');
    expect(rawBodyText({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(rawBodyText(null)).toBe('');
    expect(rawBodyText(undefined)).toBe('');
    expect(rawBodyText(42)).toBe('42');
    expect(rawBodyText(true)).toBe('true');
    expect(rawBodyText(cyclic)).toBe('');
  });

  it('pretty-prints JSON bodies and leaves other text unchanged', () => {
    expect(formattedBodyText('{"a":1,"b":[1,2]}')).toBe(
      '{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}',
    );
    expect(formattedBodyText('  not json  ')).toBe('  not json  ');
    expect(formattedBodyText({ a: 1 })).toBe('{\n  "a": 1\n}');
    for (const empty of [null, undefined, '']) {
      expect(formattedBodyText(empty)).toBe('');
    }
  });

  it('reports a parseable body only for non-empty JSON text', () => {
    expect(
      ['{"a":1}', '"a string"', '1234', 'not json', '', null, 42, { a: 1 }].map(
        hasParseableBody,
      ),
    ).toEqual([true, true, true, false, false, false, false, false]);
  });

  it('formats headers as name-value lines', () => {
    expect(
      formatHeadersForDisplay({
        'content-type': 'application/json',
        accept: ['text/plain', 'application/json'],
        'x-meta': { a: 1 },
      }),
    ).toBe(
      'content-type: application/json\n' +
        'accept: text/plain, application/json\n' +
        'x-meta: {\n  "a": 1\n}',
    );
    for (const empty of [null, undefined, 'not an object', {}]) {
      expect(formatHeadersForDisplay(empty)).toBe('');
    }
  });

  it('finds literal Unicode and punctuation without regular expressions or offset changes', () => {
    expect(bodyMatchOffsets('İ [x] 😀 [x]', '[x]')).toEqual([2, 9]);
    expect(bodyMatchOffsets('aaaa', 'aa')).toEqual([0, 2]);
    expect(bodyMatchOffsets('data', '')).toEqual([]);
  });
});

describe('debugView model probe', () => {
  it('normalizes probe Providers and resets a selection whose Provider disappears', () => {
    const state = createDebugViewState();
    state.error = 'stale';

    const providers = applyModelProbeProviders(state, {
      providers: {
        items: [
          { connections: [{ id: 'orphan' }] },
          {
            id: 'openai',
            name: 'OpenAI',
            connections: [
              { id: 'default', name: 'Default' },
              { id: 'oauth', name: '' },
            ],
          },
          { id: 'local' },
        ],
      },
    });

    expect(providers).toBe(state.modelProbeProviders);
    expect(providers).toEqual([
      {
        id: 'openai',
        name: 'OpenAI',
        connections: [
          { id: 'default', name: 'Default' },
          { id: 'oauth', name: 'oauth' },
        ],
      },
      { id: 'local', name: 'local', connections: [] },
    ]);
    expect(state.error).toBe('');

    selectModelProbeProvider(state, 'openai');
    selectModelProbeConnection(state, 'default');
    applyModelProbeProviders(state, { providers: { items: [] } });

    expect(state.modelProbeProvider).toBe('');
    expect(state.modelProbeConnection).toBe('');
  });

  it('enables probing only for a selected Provider with one of its connections', () => {
    const state = createDebugViewState();
    applyModelProbeProviders(state, {
      providers: {
        items: [
          {
            id: 'openai',
            name: 'OpenAI',
            connections: [
              { id: 'default', name: 'Default' },
              { id: 'oauth', name: 'OAuth' },
            ],
          },
        ],
      },
    });
    expect(modelProbeConnectionOptions(state)).toEqual([]);
    expect(modelProbeCanProbe(state)).toBe(false);

    state.modelProbeResult = { raw: 'previous' };
    expect(selectModelProbeProvider(state, 'openai')).toBe('openai');
    expect(state.modelProbeConnection).toBe('');
    expect(state.modelProbeResult).toBeNull();
    expect(modelProbeConnectionOptions(state)).toEqual([
      { value: 'default', label: 'Default' },
      { value: 'oauth', label: 'OAuth' },
    ]);
    expect(modelProbeCanProbe(state)).toBe(false);

    state.modelProbeError = 'previous';
    expect(selectModelProbeConnection(state, 'oauth')).toBe('oauth');
    expect(state.modelProbeError).toBe('');
    expect(modelProbeCanProbe(state)).toBe(true);

    expect(selectModelProbeConnection(state, 'missing')).toBe('');
    expect(modelProbeCanProbe(state)).toBe(false);
  });

  it('normalizes a probe result and its model preview', () => {
    const state = createDebugViewState();
    state.modelProbeLoading = true;

    expect(
      applyModelProbeResult(state, {
        raw_response: '{"data":[{"id":"gpt-5"}]}',
        status_code: 200,
        duration_ms: 250,
        trace_id: 'probe-1',
        model_preview: {
          model_count: 3,
          models: [
            { id: 'gpt-5', name: 'GPT-5' },
            { name: 'no-id' },
            { id: 'o3' },
          ],
        },
      }),
    ).toEqual({
      raw: '{"data":[{"id":"gpt-5"}]}',
      statusCode: 200,
      durationMs: 250,
      traceId: 'probe-1',
      normalized: {
        modelCount: 3,
        preview: [
          { id: 'gpt-5', name: 'GPT-5' },
          { id: 'o3', name: 'o3' },
        ],
      },
    });
    expect(state.modelProbeLoading).toBe(false);

    expect(
      applyModelProbeResult(state, { raw_response: 'plain', status_code: 500 })
        .normalized,
    ).toEqual({ modelCount: 0, preview: [] });

    state.modelProbeLoading = true;
    expect(applyModelProbeResult(state, 'oops')).toBeNull();
    expect(state.modelProbeResult).toBeNull();
    expect(state.modelProbeLoading).toBe(false);
    expect(state.modelProbeError).toBe('');
  });
});

function traceEntry(traceId) {
  return {
    trace_id: traceId,
    timestamp: '2026-05-11T10:00:00Z',
    provider_id: 'openai',
    model_id: 'gpt-5.2',
    method: 'POST',
    url: 'https://api.openai.com/v1/responses',
    status_code: 200,
    duration_ms: 100,
    type: 'provider_request',
  };
}
