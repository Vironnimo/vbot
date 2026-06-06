import { describe, expect, it } from 'vitest';

import {
  applyDebugStatus,
  applyModelProbeProviders,
  applyModelProbeResult,
  applyTraceDetail,
  applyTraceList,
  clearTracesApplied,
  createDebugViewState,
  formatHeadersForDisplay,
  formattedBodyText,
  hasParseableBody,
  isJsonParseableText,
  modelProbeCanProbe,
  modelProbeConnectionOptions,
  normalizeModelProbeProviders,
  normalizeProbePreview,
  normalizeTraceEntries,
  normalizeTraceEntry,
  rawBodyText,
  retainSelectedTrace,
  selectModelProbeConnection,
  selectModelProbeProvider,
  selectTrace,
  streamEventText,
} from '../debugView.js';

describe('debugView helpers', () => {
  describe('createDebugViewState', () => {
    it('returns a fresh state with empty defaults', () => {
      expect(createDebugViewState()).toEqual({
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
    });

    it('returns a new object each call so callers can mutate without sharing state', () => {
      const first = createDebugViewState();
      const second = createDebugViewState();
      first.traces.push({ trace_id: 'a' });
      expect(second.traces).toEqual([]);
    });
  });

  describe('applyTraceList and selection retention', () => {
    it('normalizes trace entries, clears error and loading, and keeps selection when present', () => {
      const state = createDebugViewState();
      state.loading = true;
      state.error = 'stale';

      const traces = applyTraceList(state, {
        traces: [
          {
            trace_id: 't-1',
            timestamp: '2026-05-11T10:00:00Z',
            provider_id: 'openai',
            model_id: 'gpt-5.2',
            method: 'POST',
            url: 'https://api.openai.com/v1/responses',
            status_code: 200,
            duration_ms: 1234,
            type: 'provider_request',
          },
        ],
      });

      expect(traces).toHaveLength(1);
      expect(state.traces[0]).toMatchObject({
        trace_id: 't-1',
        status_code: 200,
        duration_ms: 1234,
      });
      expect(state.loading).toBe(false);
      expect(state.error).toBe('');
    });

    it('retains a selected list entry across a fresh applyTraceList when the id is still in the list', () => {
      const state = createDebugViewState();
      applyTraceList(state, {
        traces: [traceEntry('t-1'), traceEntry('t-2')],
      });
      const original = selectTrace(state, 't-1');
      expect(original?.trace_id).toBe('t-1');

      applyTraceList(state, {
        traces: [traceEntry('t-1'), traceEntry('t-2'), traceEntry('t-3')],
      });

      expect(state.selectedTrace?.trace_id).toBe('t-1');
      expect(retainSelectedTrace(state)).toMatchObject({ trace_id: 't-1' });
    });

    it('clears the selected list entry when the id is no longer in the list', () => {
      const state = createDebugViewState();
      applyTraceList(state, { traces: [traceEntry('t-1'), traceEntry('t-2')] });
      selectTrace(state, 't-1');

      applyTraceList(state, { traces: [traceEntry('t-3')] });

      expect(state.selectedTrace).toBeNull();
    });

    it('treats a non-array result as an empty list', () => {
      const state = createDebugViewState();
      const traces = applyTraceList(state, { traces: 'nope' });
      expect(traces).toEqual([]);
      expect(state.traces).toEqual([]);
    });
  });

  describe('selectTrace', () => {
    it('selects the matching list entry by trace id', () => {
      const state = createDebugViewState();
      applyTraceList(state, {
        traces: [traceEntry('t-1'), traceEntry('t-2')],
      });

      const selected = selectTrace(state, 't-2');
      expect(selected).toMatchObject({ trace_id: 't-2' });
      expect(state.selectedTrace).toBe(selected);
    });

    it('clears the selection when the id is not in the list', () => {
      const state = createDebugViewState();
      applyTraceList(state, { traces: [traceEntry('t-1')] });

      const selected = selectTrace(state, 'missing');
      expect(selected).toBeNull();
      expect(state.selectedTrace).toBeNull();
    });

    it('treats empty/whitespace ids as missing', () => {
      const state = createDebugViewState();
      applyTraceList(state, { traces: [traceEntry('t-1')] });

      expect(selectTrace(state, '')).toBeNull();
      expect(selectTrace(state, '   ')).toBeNull();
      expect(selectTrace(state, null)).toBeNull();
    });
  });

  describe('applyTraceDetail', () => {
    it('sets the full trace and clears transient flags', () => {
      const state = createDebugViewState();
      state.loading = true;
      state.error = 'stale';

      const detail = applyTraceDetail(state, {
        trace: {
          trace_id: 't-1',
          request: { method: 'POST', body: '{"x":1}' },
          response: { status_code: 200, body: '{"y":2}' },
        },
      });

      expect(detail).toMatchObject({ trace_id: 't-1' });
      expect(state.selectedTrace).toBe(detail);
      expect(state.loading).toBe(false);
      expect(state.error).toBe('');
    });

    it('sets the selected trace to null when the result has no trace', () => {
      const state = createDebugViewState();
      applyTraceDetail(state, { trace: null });
      expect(state.selectedTrace).toBeNull();
    });
  });

  describe('clearTracesApplied', () => {
    it('empties both the list and the selection', () => {
      const state = createDebugViewState();
      applyTraceList(state, { traces: [traceEntry('t-1')] });
      selectTrace(state, 't-1');

      const traces = clearTracesApplied(state);
      expect(traces).toEqual([]);
      expect(state.traces).toEqual([]);
      expect(state.selectedTrace).toBeNull();
    });
  });

  describe('applyDebugStatus', () => {
    it('normalizes a status payload and returns it', () => {
      const state = createDebugViewState();
      const result = applyDebugStatus(state, {
        enabled: true,
        trace_limit: 100,
        trace_count: 12,
        data_directory: 'C:/data',
      });

      expect(result).toEqual({
        enabled: true,
        traceLimit: 100,
        traceCount: 12,
        dataDirectory: 'C:/data',
      });
      expect(state.error).toBe('');
      expect(state.loading).toBe(false);
    });

    it('falls back to safe defaults when fields are missing or invalid', () => {
      const state = createDebugViewState();
      const result = applyDebugStatus(state, {
        enabled: 'yes',
        trace_limit: -3,
        trace_count: 4.5,
        data_directory: null,
      });

      expect(result).toEqual({
        enabled: false,
        traceLimit: 50,
        traceCount: 0,
        dataDirectory: '',
      });
    });
  });

  describe('rawBodyText', () => {
    it('returns the original string for string bodies', () => {
      expect(rawBodyText('hello')).toBe('hello');
      expect(rawBodyText('{"a":1}')).toBe('{"a":1}');
    });

    it('returns the JSON stringification for non-string bodies', () => {
      expect(rawBodyText({ a: 1 })).toBe('{\n  "a": 1\n}');
    });

    it('returns an empty string for null and undefined', () => {
      expect(rawBodyText(null)).toBe('');
      expect(rawBodyText(undefined)).toBe('');
    });

    it('coerces numbers and booleans to their string form', () => {
      expect(rawBodyText(42)).toBe('42');
      expect(rawBodyText(true)).toBe('true');
    });

    it('returns an empty string for circular objects', () => {
      const cyclic = {};
      cyclic.self = cyclic;
      expect(rawBodyText(cyclic)).toBe('');
    });
  });

  describe('formattedBodyText and hasParseableBody', () => {
    it('pretty-prints parseable JSON strings', () => {
      expect(formattedBodyText('{"a":1,"b":[1,2]}')).toBe(
        '{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}',
      );
    });

    it('returns the original text for non-parseable strings', () => {
      expect(formattedBodyText('not json')).toBe('not json');
    });

    it('returns the original text for non-parseable whitespace-trimmed strings', () => {
      expect(formattedBodyText('  not json  ')).toBe('  not json  ');
    });

    it('returns an empty string for empty, null and undefined bodies', () => {
      expect(formattedBodyText(null)).toBe('');
      expect(formattedBodyText(undefined)).toBe('');
      expect(formattedBodyText('')).toBe('');
    });

    it('handles object bodies by stringifying with indentation', () => {
      expect(formattedBodyText({ a: 1 })).toBe('{\n  "a": 1\n}');
    });

    it('reports parseability only for non-empty JSON-valid string bodies', () => {
      expect(hasParseableBody('{"a":1}')).toBe(true);
      expect(hasParseableBody('not json')).toBe(false);
      expect(hasParseableBody('')).toBe(false);
      expect(hasParseableBody(null)).toBe(false);
      expect(hasParseableBody({ a: 1 })).toBe(false);
    });

    it('isJsonParseableText returns false for non-strings and empty strings', () => {
      expect(isJsonParseableText('')).toBe(false);
      expect(isJsonParseableText(null)).toBe(false);
      expect(isJsonParseableText(undefined)).toBe(false);
      expect(isJsonParseableText(42)).toBe(false);
      expect(isJsonParseableText('"a string"')).toBe(true);
      expect(isJsonParseableText('1234')).toBe(true);
    });
  });

  describe('formatHeadersForDisplay', () => {
    it('returns header entries as "name: value" lines', () => {
      expect(
        formatHeadersForDisplay({
          'content-type': 'application/json',
          'x-test': '1',
        }),
      ).toBe('content-type: application/json\nx-test: 1');
    });

    it('returns an empty string for null, undefined and non-objects', () => {
      expect(formatHeadersForDisplay(null)).toBe('');
      expect(formatHeadersForDisplay(undefined)).toBe('');
      expect(formatHeadersForDisplay('not an object')).toBe('');
    });

    it('returns an empty string for empty header objects', () => {
      expect(formatHeadersForDisplay({})).toBe('');
    });

    it('joins array header values with a comma and a space', () => {
      expect(
        formatHeadersForDisplay({ accept: ['text/plain', 'application/json'] }),
      ).toBe('accept: text/plain, application/json');
    });

    it('renders object header values as JSON', () => {
      const result = formatHeadersForDisplay({ 'x-meta': { a: 1 } });
      expect(result).toBe('x-meta: {\n  "a": 1\n}');
    });
  });

  describe('streamEventText', () => {
    it('returns string events as-is', () => {
      expect(streamEventText('event: foo\ndata: bar\n\n')).toBe(
        'event: foo\ndata: bar\n\n',
      );
    });

    it('returns an empty string for null and undefined', () => {
      expect(streamEventText(null)).toBe('');
      expect(streamEventText(undefined)).toBe('');
    });

    it('renders object events as JSON', () => {
      expect(streamEventText({ event: 'foo', data: 'bar' })).toBe(
        '{\n  "event": "foo",\n  "data": "bar"\n}',
      );
    });
  });

  describe('retainSelectedTrace', () => {
    it('returns null when there is no current selection', () => {
      const state = createDebugViewState();
      applyTraceList(state, { traces: [traceEntry('t-1')] });
      expect(retainSelectedTrace(state)).toBeNull();
    });

    it('returns null when the previously selected id is not in the list', () => {
      const state = createDebugViewState();
      state.selectedTrace = traceEntry('t-missing');
      expect(retainSelectedTrace(state)).toBeNull();
    });

    it('finds the matching list entry when the id is present', () => {
      const state = createDebugViewState();
      applyTraceList(state, {
        traces: [traceEntry('t-1'), traceEntry('t-2')],
      });
      state.selectedTrace = { trace_id: 't-2' };
      const result = retainSelectedTrace(state);
      expect(result).toMatchObject({ trace_id: 't-2' });
    });
  });

  describe('trace entry normalization', () => {
    it('drops entries without a trace_id', () => {
      expect(normalizeTraceEntries([{}, { trace_id: 't-1' }])).toHaveLength(1);
    });

    it('coerces non-integer status and duration to null', () => {
      const entry = normalizeTraceEntry({
        trace_id: 't-1',
        status_code: 'oops',
        duration_ms: 12.5,
      });
      expect(entry?.status_code).toBeNull();
      expect(entry?.duration_ms).toBeNull();
    });

    it('preserves nullable integers when null', () => {
      const entry = normalizeTraceEntry({ trace_id: 't-1' });
      expect(entry?.status_code).toBeNull();
      expect(entry?.duration_ms).toBeNull();
    });

    it('returns null for entries without a trace_id', () => {
      expect(normalizeTraceEntry(null)).toBeNull();
      expect(normalizeTraceEntry({})).toBeNull();
    });
  });

  describe('model probe providers', () => {
    it('normalizes provider and connection identifiers', () => {
      const result = normalizeModelProbeProviders([
        {
          id: 'openai',
          name: 'OpenAI',
          connections: [
            { id: 'default', name: 'Default' },
            { id: 'oauth', name: '' },
          ],
        },
      ]);
      expect(result).toEqual([
        {
          id: 'openai',
          name: 'OpenAI',
          connections: [
            { id: 'default', name: 'Default' },
            { id: 'oauth', name: 'oauth' },
          ],
        },
      ]);
    });

    it('drops providers and connections that lack an id', () => {
      const result = normalizeModelProbeProviders([
        { connections: [{ name: 'orphan' }] },
        { id: 'openai', connections: [{ id: 'default' }] },
      ]);
      expect(result).toEqual([
        {
          id: 'openai',
          name: 'openai',
          connections: [{ id: 'default', name: 'default' }],
        },
      ]);
    });

    it('falls back to id for missing provider name', () => {
      const result = normalizeModelProbeProviders([{ id: 'openai' }]);
      expect(result[0].name).toBe('openai');
    });

    it('applyModelProbeProviders resets the selected provider/connection when it disappears', () => {
      const state = createDebugViewState();
      applyModelProbeProviders(state, {
        providers: {
          items: [
            {
              id: 'openai',
              name: 'OpenAI',
              connections: [{ id: 'default', name: 'Default' }],
            },
          ],
        },
      });
      selectModelProbeProvider(state, 'openai');
      selectModelProbeConnection(state, 'default');

      applyModelProbeProviders(state, { providers: { items: [] } });

      expect(state.modelProbeProvider).toBe('');
      expect(state.modelProbeConnection).toBe('');
    });

    it('selectModelProbeProvider and selectModelProbeConnection store the chosen id or empty string', () => {
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

      expect(selectModelProbeProvider(state, 'openai')).toBe('openai');
      expect(state.modelProbeConnection).toBe('');

      expect(selectModelProbeConnection(state, 'oauth')).toBe('oauth');
      expect(state.modelProbeResult).toBeNull();
      expect(state.modelProbeError).toBe('');

      expect(selectModelProbeConnection(state, 'missing')).toBe('');
    });

    it('modelProbeCanProbe requires both a provider and a valid connection', () => {
      const state = createDebugViewState();
      applyModelProbeProviders(state, {
        providers: {
          items: [
            {
              id: 'openai',
              name: 'OpenAI',
              connections: [{ id: 'default', name: 'Default' }],
            },
          ],
        },
      });
      expect(modelProbeCanProbe(state)).toBe(false);
      selectModelProbeProvider(state, 'openai');
      expect(modelProbeCanProbe(state)).toBe(false);
      selectModelProbeConnection(state, 'default');
      expect(modelProbeCanProbe(state)).toBe(true);
    });

    it('modelProbeConnectionOptions emits value/label pairs and hides when no provider', () => {
      const state = createDebugViewState();
      expect(modelProbeConnectionOptions(state)).toEqual([]);

      applyModelProbeProviders(state, {
        providers: {
          items: [
            {
              id: 'openai',
              name: 'OpenAI',
              connections: [{ id: 'default', name: 'Default' }],
            },
          ],
        },
      });
      selectModelProbeProvider(state, 'openai');

      expect(modelProbeConnectionOptions(state)).toEqual([
        { value: 'default', label: 'Default' },
      ]);
    });
  });

  describe('applyModelProbeResult', () => {
    it('returns a null result and clears the loading flag when the payload is not an object', () => {
      const state = createDebugViewState();
      state.modelProbeLoading = true;

      const result = applyModelProbeResult(state, 'oops');
      expect(result).toBeNull();
      expect(state.modelProbeResult).toBeNull();
      expect(state.modelProbeLoading).toBe(false);
      expect(state.modelProbeError).toBe('');
    });

    it('normalizes a 200 result with a parseable preview', () => {
      const state = createDebugViewState();
      const result = applyModelProbeResult(state, {
        raw_response: '{"data":[{"id":"gpt-5"}]}',
        status_code: 200,
        duration_ms: 250,
        trace_id: 'probe-1',
        model_preview: {
          model_count: 1,
          models: [{ id: 'gpt-5', name: 'GPT-5' }],
        },
      });

      expect(result).toEqual({
        raw: '{"data":[{"id":"gpt-5"}]}',
        statusCode: 200,
        durationMs: 250,
        traceId: 'probe-1',
        normalized: {
          modelCount: 1,
          preview: [{ id: 'gpt-5', name: 'GPT-5' }],
        },
      });
    });

    it('falls back to an empty preview when model_preview is missing', () => {
      const state = createDebugViewState();
      const result = applyModelProbeResult(state, {
        raw_response: 'plain text',
        status_code: 500,
        duration_ms: 12,
        trace_id: 'probe-2',
      });

      expect(result.normalized).toEqual({ modelCount: 0, preview: [] });
      expect(result.statusCode).toBe(500);
    });
  });

  describe('normalizeProbePreview', () => {
    it('returns empty defaults for non-objects', () => {
      expect(normalizeProbePreview(null)).toEqual({
        modelCount: 0,
        preview: [],
      });
      expect(normalizeProbePreview('oops')).toEqual({
        modelCount: 0,
        preview: [],
      });
    });

    it('drops preview entries that lack an id', () => {
      const result = normalizeProbePreview({
        model_count: 2,
        models: [{ id: 'a' }, { name: 'no-id' }, { id: 'b', name: 'B' }],
      });
      expect(result.preview).toEqual([
        { id: 'a', name: 'a' },
        { id: 'b', name: 'B' },
      ]);
      expect(result.modelCount).toBe(2);
    });
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
