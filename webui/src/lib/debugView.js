import { asOptionalText, asText, isPlainObject } from './values.js';
import { activeLocaleTag, t } from './i18n.js';
import { formatMoment } from './timeText.js';

export function createDebugViewState() {
  return {
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
  };
}

export function applyTraceList(state, result) {
  const rawTraces = Array.isArray(result?.traces) ? result.traces : [];
  state.traces = normalizeTraceEntries(rawTraces);
  state.selectedTrace = retainSelectedTrace(state);
  state.loading = false;
  state.error = '';
  return state.traces;
}

export function applyTraceDetail(state, result) {
  const trace = isPlainObject(result?.trace) ? result.trace : null;
  state.selectedTrace = trace;
  state.loading = false;
  state.error = '';
  return state.selectedTrace;
}

export function selectTrace(state, traceId) {
  const normalizedId = asOptionalText(traceId);
  const traces = Array.isArray(state?.traces) ? state.traces : [];
  const selectedTrace =
    normalizedId !== null &&
    traces.some((trace) => trace.trace_id === normalizedId)
      ? (traces.find((trace) => trace.trace_id === normalizedId) ?? null)
      : null;

  state.selectedTrace = selectedTrace;
  return state.selectedTrace;
}

export function clearTracesApplied(state) {
  state.traces = [];
  state.selectedTrace = null;
  return state.traces;
}

export function applyDebugStatus(state, result) {
  state.error = '';
  state.loading = false;
  return {
    enabled: resolveBoolean(result?.enabled, false),
    traceLimit: resolvePositiveInteger(result?.trace_limit, 50),
    traceCount: resolveNonNegativeInteger(result?.trace_count, 0),
    dataDirectory: asText(result?.data_directory),
  };
}

export function applyModelProbeProviders(state, result) {
  const providerItems = Array.isArray(result?.providers?.items)
    ? result.providers.items
    : [];
  state.modelProbeProviders = normalizeModelProbeProviders(providerItems);
  state.error = '';

  if (
    state.modelProbeProvider &&
    !state.modelProbeProviders.some((p) => p.id === state.modelProbeProvider)
  ) {
    state.modelProbeProvider = '';
    state.modelProbeConnection = '';
  }

  return state.modelProbeProviders;
}

export function selectModelProbeProvider(state, providerId) {
  const normalizedId = asOptionalText(providerId);
  const providers = Array.isArray(state?.modelProbeProviders)
    ? state.modelProbeProviders
    : [];
  const provider =
    normalizedId !== null
      ? (providers.find((p) => p.id === normalizedId) ?? null)
      : null;

  state.modelProbeProvider = provider ? provider.id : '';
  state.modelProbeConnection = '';
  state.modelProbeResult = null;
  state.modelProbeError = '';

  return state.modelProbeProvider;
}

export function selectModelProbeConnection(state, connectionId) {
  const normalizedId = asOptionalText(connectionId);
  const provider = resolveSelectedProbeProvider(state);

  if (!provider || normalizedId === null) {
    state.modelProbeConnection = '';
    return state.modelProbeConnection;
  }

  const connections = Array.isArray(provider.connections)
    ? provider.connections
    : [];
  const connection = connections.find((c) => c.id === normalizedId);

  state.modelProbeConnection = connection ? connection.id : '';
  state.modelProbeResult = null;
  state.modelProbeError = '';

  return state.modelProbeConnection;
}

export function applyModelProbeResult(state, result) {
  state.modelProbeLoading = false;
  state.modelProbeError = '';

  if (!isPlainObject(result)) {
    state.modelProbeResult = null;
    return state.modelProbeResult;
  }

  const normalizedResult = {
    raw: asText(result.raw_response),
    statusCode: resolveNonNegativeInteger(result.status_code, 0),
    durationMs: resolveNonNegativeInteger(result.duration_ms, 0),
    traceId: asText(result.trace_id),
    normalized: isPlainObject(result.model_preview)
      ? normalizeProbePreview(result.model_preview)
      : { modelCount: 0, preview: [] },
  };

  state.modelProbeResult = normalizedResult;
  return state.modelProbeResult;
}

export function modelProbeCanProbe(state) {
  const provider = resolveSelectedProbeProvider(state);
  if (!provider) {
    return false;
  }

  const connectionId = asOptionalText(state?.modelProbeConnection);
  if (connectionId === null) {
    return false;
  }

  const connections = Array.isArray(provider.connections)
    ? provider.connections
    : [];
  return connections.some((c) => c.id === connectionId);
}

export function modelProbeConnectionOptions(state) {
  const provider = resolveSelectedProbeProvider(state);
  if (!provider) {
    return [];
  }

  const connections = Array.isArray(provider.connections)
    ? provider.connections
    : [];

  return connections.map((connection) => ({
    value: connection.id,
    label: asText(connection.name) || connection.id,
  }));
}

function normalizeTraceEntries(traces) {
  return traces
    .map((trace) => normalizeTraceEntry(trace))
    .filter((trace) => trace !== null);
}

function normalizeTraceEntry(trace) {
  const traceId = asOptionalText(trace?.trace_id);
  if (traceId === null) {
    return null;
  }

  return {
    trace_id: traceId,
    timestamp: asText(trace?.timestamp),
    provider_id: asText(trace?.provider_id),
    model_id: asText(trace?.model_id),
    method: asText(trace?.method),
    url: asText(trace?.url),
    status_code: resolveNullableInteger(trace?.status_code),
    duration_ms: resolveNullableInteger(trace?.duration_ms),
    type: asOptionalText(trace?.type),
  };
}

function normalizeModelProbeProviders(providers) {
  return providers
    .map((provider) => {
      const id = asOptionalText(provider?.id ?? provider?.provider_id);
      if (id === null) {
        return null;
      }

      const name = asOptionalText(provider?.name);
      const rawConnections = Array.isArray(provider?.connections)
        ? provider.connections
        : [];

      const connections = rawConnections
        .map((connection) => {
          const connectionId = asOptionalText(
            connection?.id ?? connection?.connection_id,
          );
          if (connectionId === null) {
            return null;
          }

          return {
            id: connectionId,
            name: asOptionalText(connection?.name) ?? connectionId,
          };
        })
        .filter((connection) => connection !== null);

      return {
        id,
        name: name ?? id,
        connections,
      };
    })
    .filter((provider) => provider !== null);
}

function normalizeProbePreview(normalized) {
  const modelCount = resolveNonNegativeInteger(normalized.model_count, 0);
  const rawPreview = Array.isArray(normalized.models) ? normalized.models : [];

  const preview = rawPreview
    .map((model) => {
      if (!isPlainObject(model)) {
        return null;
      }

      const modelId = asOptionalText(model.id);
      if (modelId === null) {
        return null;
      }

      return {
        id: modelId,
        name: asOptionalText(model.name) ?? modelId,
      };
    })
    .filter((model) => model !== null);

  return {
    modelCount,
    preview,
  };
}

function resolveSelectedProbeProvider(state) {
  const providerId = asOptionalText(state?.modelProbeProvider);
  if (providerId === null) {
    return null;
  }

  const providers = Array.isArray(state?.modelProbeProviders)
    ? state.modelProbeProviders
    : [];

  return providers.find((p) => p.id === providerId) ?? null;
}

function retainSelectedTrace(state) {
  const traces = state.traces;
  const currentSelection = state?.selectedTrace;
  const selectedId = asOptionalText(currentSelection?.trace_id);
  if (selectedId === null) {
    return null;
  }
  return traces.some((trace) => trace.trace_id === selectedId)
    ? currentSelection
    : null;
}

function isJsonParseableText(value) {
  try {
    JSON.parse(value);
    return true;
  } catch {
    return false;
  }
}

export function rawBodyText(body) {
  if (body === null || body === undefined) {
    return '';
  }
  if (typeof body === 'string') {
    return body;
  }
  if (typeof body === 'object') {
    return safeStringify(body);
  }
  return String(body);
}

export function formattedBodyText(body) {
  if (body === null || body === undefined || body === '') {
    return '';
  }
  if (typeof body === 'string') {
    if (!isJsonParseableText(body)) {
      return body;
    }
    return JSON.stringify(JSON.parse(body), null, 2);
  }
  if (typeof body === 'object') {
    return safeStringify(body);
  }
  return String(body);
}

export function hasParseableBody(body) {
  if (typeof body !== 'string' || body.length === 0) {
    return false;
  }
  return isJsonParseableText(body);
}

export function formatHeadersForDisplay(headers) {
  if (!isPlainObject(headers)) {
    return '';
  }
  const entries = Object.entries(headers);
  if (entries.length === 0) {
    return '';
  }
  return entries
    .map(([name, value]) => `${name}: ${formatHeaderValue(value)}`)
    .join('\n');
}

function formatHeaderValue(value) {
  if (value === null || value === undefined) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => formatHeaderValue(item)).join(', ');
  }
  if (typeof value === 'object') {
    return safeStringify(value);
  }
  return String(value);
}

function safeStringify(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '';
  }
}

function resolveBoolean(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}

function resolvePositiveInteger(value, fallback) {
  const numberValue = Number(value);
  return Number.isInteger(numberValue) && numberValue > 0
    ? numberValue
    : fallback;
}

function resolveNonNegativeInteger(value, fallback) {
  const numberValue = Number(value);
  return Number.isInteger(numberValue) && numberValue >= 0
    ? numberValue
    : fallback;
}

function resolveNullableInteger(value) {
  if (value === null || value === undefined || value === '') return null;
  const numberValue = Number(value);
  return Number.isInteger(numberValue) ? numberValue : null;
}

// The list index does not contain capture errors: a missing status is unknown,
// and even a 2xx/101 response may have failed later while streaming.
export function traceStatusTone(status) {
  if (status === null || status === undefined) return 'unknown';
  if (status >= 400) return 'error';
  if ((status >= 200 && status < 300) || status === 101) return 'ok';
  return 'unknown';
}

export function filterTraces(
  traces,
  query = '',
  status = 'all',
  provider = '',
) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return traces.filter((trace) => {
    if (provider && trace.provider_id !== provider) return false;
    if (status !== 'all' && traceStatusTone(trace.status_code) !== status)
      return false;
    const haystack = [
      trace.trace_id,
      trace.provider_id,
      trace.model_id,
      trace.method,
      trace.url,
      trace.timestamp,
      trace.status_code,
      trace.type,
    ]
      .join(' ')
      .toLowerCase();
    return words.every((word) => haystack.includes(word));
  });
}

export function bodyMatchOffsets(text, query) {
  if (!query) return [];
  const matches = [];
  // Case-sensitive matching keeps offsets exact even for Unicode case mappings.
  for (
    let at = text.indexOf(query);
    at !== -1;
    at = text.indexOf(query, at + query.length)
  ) {
    matches.push(at);
  }
  return matches;
}

// ---------------------------------------------------------------------------
// Trace names and the trace details card. A list entry carries `url`,
// `method` and `status_code`; a trace detail carries them under `request` and
// `response`. Both shapes read the same here.

const TRACE_TYPE_LABELS = Object.freeze({
  provider_request: () => t('debug.providerRequest'),
  model_probe: () => t('debug.modelProbe'),
});

// Standard reason phrases for the status codes Provider APIs commonly return.
const HTTP_REASON_PHRASES = Object.freeze({
  101: 'Switching Protocols',
  200: 'OK',
  201: 'Created',
  202: 'Accepted',
  204: 'No Content',
  301: 'Moved Permanently',
  302: 'Found',
  304: 'Not Modified',
  400: 'Bad Request',
  401: 'Unauthorized',
  402: 'Payment Required',
  403: 'Forbidden',
  404: 'Not Found',
  405: 'Method Not Allowed',
  408: 'Request Timeout',
  409: 'Conflict',
  413: 'Payload Too Large',
  415: 'Unsupported Media Type',
  422: 'Unprocessable Entity',
  429: 'Too Many Requests',
  500: 'Internal Server Error',
  502: 'Bad Gateway',
  503: 'Service Unavailable',
  504: 'Gateway Timeout',
  529: 'Overloaded',
});

const STATUS_ROW_TONES = Object.freeze({ ok: 'success', error: 'danger' });

function traceUrl(trace) {
  return asText(trace?.url) || asText(trace?.request?.url);
}

function traceMethod(trace) {
  return asText(trace?.method) || asText(trace?.request?.method);
}

function traceStatusCode(trace) {
  return resolveNullableInteger(
    trace?.status_code ?? trace?.response?.status_code,
  );
}

function urlPath(url) {
  if (!url) return '';
  try {
    return new URL(url).pathname;
  } catch {
    return '';
  }
}

/** A trace type as a readable label; unknown types stay raw. */
export function traceTypeLabel(type) {
  const code = asText(type);
  return Object.hasOwn(TRACE_TYPE_LABELS, code)
    ? TRACE_TYPE_LABELS[code]()
    : code;
}

/**
 * The name a trace goes by: its Model id, "Model Probe", or the request path
 * of a Provider request without a Model. `mono` marks the request path.
 */
export function traceLabel(trace) {
  const modelId = asText(trace?.model_id);
  if (modelId) return { text: modelId, mono: false };
  if (trace?.type === 'model_probe') {
    return { text: t('debug.modelProbe'), mono: false };
  }
  const path = urlPath(traceUrl(trace));
  return path
    ? { text: path, mono: true }
    : {
        text: traceTypeLabel(trace?.type) || t('debug.providerRequest'),
        mono: false,
      };
}

/** An HTTP status with its reason phrase, such as "429 Too Many Requests". */
export function formatTraceStatus(status) {
  const code = resolveNullableInteger(status);
  if (code === null) return '';
  const reason = HTTP_REASON_PHRASES[code];
  return reason ? `${code} ${reason}` : String(code);
}

/**
 * The details card of one trace: its name, what kind of request it was, and
 * the request line, status, start, exact duration and ids as rows.
 */
export function traceTooltip(trace, nowMs = Date.now()) {
  if (!trace) return '';
  const label = traceLabel(trace);
  const typeLabel = traceTypeLabel(trace.type);
  const status = traceStatusCode(trace);
  const duration = resolveNullableInteger(trace.duration_ms);
  return {
    title: label.text,
    text: typeLabel && typeLabel !== label.text ? typeLabel : '',
    rows: [
      {
        label: t('debug.modelProbe.provider'),
        value: asText(trace.provider_id),
      },
      {
        label: t('debug.request'),
        value: [traceMethod(trace), traceUrl(trace)].filter(Boolean).join(' '),
        mono: true,
      },
      {
        label: t('debug.responseStatus'),
        value: formatTraceStatus(status),
        tone: STATUS_ROW_TONES[traceStatusTone(status)] ?? '',
      },
      {
        label: t('debug.started'),
        value: formatMoment(trace.timestamp, { nowMs, seconds: true }),
      },
      {
        label: t('debug.duration'),
        value:
          duration === null
            ? ''
            : `${new Intl.NumberFormat(activeLocaleTag()).format(duration)} ms`,
      },
      { label: t('debug.traceType'), value: asText(trace.type), mono: true },
      { label: t('debug.traceId'), value: asText(trace.trace_id), mono: true },
    ],
    placement: 'right',
  };
}
